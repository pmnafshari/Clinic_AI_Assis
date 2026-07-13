import json
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

from werkzeug.security import generate_password_hash

import agent
import app.agent_routes as agent_routes
import app.db as app_db
import pending_actions
from app import create_app
from web_session import _hash_token


def _seed_user(db_path, username, password, role):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO users (username, password_hash, role, active) VALUES (?, ?, ?, 1)",
        (username, generate_password_hash(password), role),
    )
    conn.commit()
    conn.close()


def _csrf_from(html):
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return match.group(1)


def _token_from(html):
    match = re.search(r'name="token" value="([^"]+)"', html)
    return match.group(1)


def _login(app, username, password):
    client = app.test_client()
    get_resp = client.get("/login")
    csrf = _csrf_from(get_resp.text)
    login_resp = client.post(
        "/login", data={"username": username, "password": password, "csrf_token": csrf}
    )
    assert login_resp.status_code == 302, f"login for {username} should redirect"
    return client


def fake_urlopen(req, timeout=120):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def read(self):
            tool_call = {
                "tool": "update_field",
                "args": {"patient": "rossi", "field": "phone", "value": "333-1234"},
            }
            return json.dumps({"response": json.dumps(tool_call)}).encode()

    return FakeResponse()


def _phone(db_path, cf):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT phone FROM patients WHERE codice_fiscale = ?", (cf,)).fetchone()
    conn.close()
    return row["phone"]


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "clinic.sqlite")
        app_db.DB_PATH = db_path
        agent_routes.CHROMA_PATH = str(Path(tmp) / "chroma")
        agent_routes.UNDO_LOG = str(Path(tmp) / "undo_log.jsonl")

        app = create_app()
        app.config["TESTING"] = True
        agent_routes._urlopen = fake_urlopen

        _seed_user(db_path, "drossi", "goodpass", "dentist")
        _seed_user(db_path, "drossi2", "goodpass", "dentist")

        cf = "RSSM800010150100"
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO patients (codice_fiscale, patient_name, phone) VALUES (?, ?, ?)",
            (cf, "mario rossi", "333 9999999"),
        )
        conn.commit()
        conn.close()

        client = _login(app, "drossi", "goodpass")

        # 1. GUI-04 - building the change via /agent/edit renders the old->new
        # diff and does not write to the db before confirm
        edit_get = client.get("/agent/edit")
        assert edit_get.status_code == 200, "GET /agent/edit should return 200"
        csrf_edit = _csrf_from(edit_get.text)
        build_resp = client.post(
            "/agent/edit",
            data={"patient": "rossi", "field": "phone", "value": "333-1234", "csrf_token": csrf_edit},
        )
        assert build_resp.status_code == 200, "build via /agent/edit should return 200"
        assert "333 9999999" in build_resp.text, "GUI-04: diff should show the current value"
        assert "333-1234" in build_resp.text, "GUI-04: diff should show the new value"
        assert 'name="token"' in build_resp.text, "confirm page should carry a hidden token"
        assert _phone(db_path, cf) == "333 9999999", "GUI-04: building the diff must not write to the db"

        token = _token_from(build_resp.text)
        csrf_confirm = _csrf_from(build_resp.text)

        # 2. D-04 - confirm applies exactly the frozen payload
        confirm_resp = client.post("/agent/confirm", data={"token": token, "csrf_token": csrf_confirm})
        assert confirm_resp.status_code == 302, "confirm should redirect on success"
        assert _phone(db_path, cf) == "333-1234", "confirm should apply the frozen new value"

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        allowed_rows = conn.execute(
            "SELECT * FROM audit_log WHERE action = 'update_field' AND allowed = 1"
        ).fetchall()
        conn.close()
        assert len(allowed_rows) == 1, "confirm should log exactly one allowed row"

        # 3. consumed/replay - a second confirm with the same token re-renders
        # the expired/invalid message and does not double-apply
        replay_resp = client.post("/agent/confirm", data={"token": token, "csrf_token": csrf_confirm})
        assert replay_resp.status_code == 200, "replayed confirm should re-render, not redirect"
        assert "expired" in replay_resp.text.lower(), "replayed confirm should show the expiry/invalid message"
        assert _phone(db_path, cf) == "333-1234", "replayed confirm must not double-apply"

        # 4. RBAC-01/SC4 - a valid token confirmed by a role lacking permission
        # is denied inside apply_pending_action, not just hidden by the UI.
        # Build as dentist (authorized), then simulate a role change between
        # build and confirm by downgrading the live session's role directly.
        edit_get2 = client.get("/agent/edit")
        csrf_edit2 = _csrf_from(edit_get2.text)
        build_resp2 = client.post(
            "/agent/edit",
            data={"patient": "rossi", "field": "phone", "value": "555-0000", "csrf_token": csrf_edit2},
        )
        token2 = _token_from(build_resp2.text)
        csrf_confirm2 = _csrf_from(build_resp2.text)

        raw_session_token = client.get_cookie("session_token").value
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE sessions SET role = 'assistant' WHERE token_hash = ?",
            (_hash_token(raw_session_token),),
        )
        conn.commit()
        conn.close()

        denied_resp = client.post("/agent/confirm", data={"token": token2, "csrf_token": csrf_confirm2})
        assert denied_resp.status_code == 200, "denied confirm should re-render, not redirect"
        assert "permission" in denied_resp.text.lower(), "denied confirm should show a permission message"
        assert _phone(db_path, cf) == "333-1234", "RBAC-01/SC4: denied confirm must not change the phone"

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        denied_rows = conn.execute(
            "SELECT * FROM audit_log WHERE action = 'update_field' AND allowed = 0"
        ).fetchall()
        conn.close()
        assert len(denied_rows) == 1, "RBAC-01/SC4: denied confirm should log exactly one denied row"
        assert denied_rows[0]["username"] == "drossi", "denied row should carry the acting username"

        # restore the live session's role for the remaining assertions
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE sessions SET role = 'dentist' WHERE token_hash = ?",
            (_hash_token(raw_session_token),),
        )
        conn.commit()
        conn.close()

        # 4b. the denied confirm consumed the token: replaying it with the
        # role restored must render expired and still apply nothing
        replay_denied = client.post("/agent/confirm", data={"token": token2, "csrf_token": csrf_confirm2})
        assert replay_denied.status_code == 200, "replay of a denied token should re-render"
        assert "expired" in replay_denied.text.lower(), \
            "a denied confirm must consume the token - replay should show expired"
        assert _phone(db_path, cf) == "333-1234", \
            "a consumed denied token must not apply after a role restore"

        # 5. D-05 - an expired pending action re-renders with the expiry
        # message and applies nothing
        edit_get3 = client.get("/agent/edit")
        csrf_edit3 = _csrf_from(edit_get3.text)
        build_resp3 = client.post(
            "/agent/edit",
            data={"patient": "rossi", "field": "phone", "value": "666-6666", "csrf_token": csrf_edit3},
        )
        token3 = _token_from(build_resp3.text)
        csrf_confirm3 = _csrf_from(build_resp3.text)

        old_expiry = pending_actions.PENDING_ACTION_EXPIRY_MINUTES
        pending_actions.PENDING_ACTION_EXPIRY_MINUTES = -1
        try:
            expired_resp = client.post("/agent/confirm", data={"token": token3, "csrf_token": csrf_confirm3})
        finally:
            pending_actions.PENDING_ACTION_EXPIRY_MINUTES = old_expiry

        assert expired_resp.status_code == 200, "expired confirm should re-render, not redirect"
        assert "expired" in expired_resp.text.lower(), "D-05: expired confirm should show the expiry message"
        assert _phone(db_path, cf) == "333-1234", "D-05: an expired token must apply nothing"

        # 6. GUI-05 - /agent/undo reverts only the acting user's own last change
        agent.write_undo_entry(
            {
                "ts": "2026-01-01T00:00:00",
                "tool": "update_field",
                "codice_fiscale": cf,
                "target": "sqlite:patients.phone",
                "before": "111-1111",
                "username": "drossi",
            },
            agent_routes.UNDO_LOG,
        )
        agent.write_undo_entry(
            {
                "ts": "2026-01-01T00:00:01",
                "tool": "update_field",
                "codice_fiscale": cf,
                "target": "sqlite:patients.phone",
                "before": "222-2222",
                "username": "drossi2",
            },
            agent_routes.UNDO_LOG,
        )

        client2 = _login(app, "drossi2", "goodpass")
        dash_resp = client2.get("/")
        csrf_undo = _csrf_from(dash_resp.text)
        undo_resp = client2.post("/agent/undo", data={"csrf_token": csrf_undo})
        assert undo_resp.status_code == 302, "undo should redirect"
        assert _phone(db_path, cf) == "222-2222", \
            "GUI-05: undo should restore the acting user's own before-value"

        remaining = [json.loads(l) for l in Path(agent_routes.UNDO_LOG).read_text().strip().splitlines()]
        remaining_usernames = [e.get("username") for e in remaining]
        assert "drossi2" not in remaining_usernames, "GUI-05: the acting user's entry must be removed"
        assert "drossi" in remaining_usernames, "GUI-05: another user's entry must remain untouched"

        # 7. an ambiguous patient name never reaches input() - the page
        # re-renders with a clear message and no confirm token
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO patients (codice_fiscale, patient_name, phone) VALUES (?, ?, ?)",
            ("RSSA850010150200", "anna rossi", "333 8888888"),
        )
        conn.commit()
        conn.close()

        edit_get4 = client.get("/agent/edit")
        csrf_edit4 = _csrf_from(edit_get4.text)
        ambiguous_resp = client.post(
            "/agent/edit",
            data={"patient": "rossi", "field": "phone", "value": "777-7777", "csrf_token": csrf_edit4},
        )
        assert ambiguous_resp.status_code == 200, "ambiguous name should re-render, not hang or 500"
        assert "Multiple patients match" in ambiguous_resp.text, \
            "ambiguous name should show the be-more-specific message"
        assert 'name="token"' not in ambiguous_resp.text, "ambiguous name must not produce a confirm token"
        assert _phone(db_path, cf) == "222-2222", "ambiguous name must not change any patient"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python agent_routes_selftest.py --selftest")


if __name__ == "__main__":
    main()
