import re
import sqlite3
import sys
import tempfile
from pathlib import Path

from werkzeug.security import generate_password_hash

import app.agent_routes as agent_routes
import app.db as app_db
from app import create_app


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


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "clinic.sqlite")
        app_db.DB_PATH = db_path
        app_db.CHROMA_PATH = str(Path(tmp) / "chroma")
        app_db._collection_cache = None
        agent_routes.UNDO_LOG = str(Path(tmp) / "undo_log.jsonl")

        app = create_app()
        app.config["TESTING"] = True

        # 1. the app boots clean with the vendored shell in place
        client = app.test_client()

        # 2. default-deny holds for /patients before login. patients_bp
        # doesn't exist yet (lands in plan 10.1-03) so this is a 404 today;
        # once the route is registered it becomes a 302 to /login. either
        # way an unauthenticated request must never reach patient data.
        deny_resp = client.get("/patients")
        assert deny_resp.status_code in (302, 404), \
            "unauthenticated /patients must never return 200"
        if deny_resp.status_code == 302:
            assert "/login" in deny_resp.headers["Location"], \
                "a redirect from /patients must target /login"

        # 3. vendored htmx serves straight off /static - no CDN round trip
        htmx_resp = client.get("/static/vendor/htmx/1.9.12/htmx.min.js")
        assert htmx_resp.status_code == 200, "vendored htmx.min.js should serve 200"

    print("selftest ok")

    # -----------------------------------------------------------------
    # 10.1-03 / 10.1-04 append here: once patients_bp exists, add
    # list_view/search_fragment/detail_view assertions - RBAC (dentist
    # vs assistant vs admin), fuzzy search candidates, CSRF on the edit
    # modal, confirm-diff swap.
    # -----------------------------------------------------------------

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "clinic.sqlite")
        app_db.DB_PATH = db_path
        app_db.CHROMA_PATH = str(Path(tmp) / "chroma")
        app_db._collection_cache = None
        agent_routes.UNDO_LOG = str(Path(tmp) / "undo_log.jsonl")

        app = create_app()
        app.config["TESTING"] = True

        _seed_user(db_path, "drossi", "goodpass", "dentist")
        _seed_user(db_path, "aassist", "goodpass", "assistant")
        _seed_user(db_path, "aadmin", "goodpass", "admin")

        cf = "RSSM800010150100"
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO patients (codice_fiscale, patient_name, phone) VALUES (?, ?, ?)",
            (cf, "mario rossi", "333123456"),
        )
        conn.execute(
            "INSERT INTO visits"
            " (codice_fiscale, visit_date, procedures, clinical_notes, next_appointment, source_path)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (cf, "2026-06-01", '["rct 26"]', "rct done on tooth 26", "2026-08-01", "n1.json"),
        )
        conn.commit()
        conn.close()

        # 1. RBAC-04 - admin gets no table, just the redirect + denied audit row
        admin_client = _login(app, "aadmin", "goodpass")
        admin_list_resp = admin_client.get("/patients")
        assert admin_list_resp.status_code == 302, \
            "admin GET /patients should redirect (RBAC-04)"
        assert "mario rossi" not in admin_list_resp.text, \
            "admin must never see the patient table"

        admin_detail_resp = admin_client.get(f"/patients/{cf}")
        assert admin_detail_resp.status_code == 302, \
            "admin GET /patients/<cf> should redirect (RBAC-04)"

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        admin_denied_rows = conn.execute(
            "SELECT * FROM audit_log WHERE username = 'aadmin' AND action = 'read_notes' AND allowed = 0"
        ).fetchall()
        conn.close()
        assert len(admin_denied_rows) == 2, \
            f"RBAC-04: expected 2 denied read_notes rows for admin, got {len(admin_denied_rows)}"

        # 2. RBAC-03 - the clinical card is present for dentist, entirely
        # absent (raw bytes, not CSS) for assistant
        dentist_client = _login(app, "drossi", "goodpass")
        dentist_detail_resp = dentist_client.get(f"/patients/{cf}")
        assert dentist_detail_resp.status_code == 200
        assert "rct done on tooth 26" in dentist_detail_resp.text, \
            "dentist (read_clinical) should see the clinical card"

        assistant_client = _login(app, "aassist", "goodpass")
        assistant_detail_resp = assistant_client.get(f"/patients/{cf}")
        assert assistant_detail_resp.status_code == 200
        assert "mario rossi" in assistant_detail_resp.text, \
            "assistant should still see the CRM card"
        assert "rct done on tooth 26" not in assistant_detail_resp.text, \
            "assistant (read_notes but not read_clinical) must not see clinical text - RBAC-03"

        # 3. SC2 - fuzzy search returns the seeded candidate for a typo,
        # and the neutral no-match copy for a miss
        typo_resp = dentist_client.get("/patients/search?q=rosi")
        assert typo_resp.status_code == 200
        assert "mario rossi" in typo_resp.text, \
            "typo query 'rosi' should surface the seeded patient"

        miss_resp = dentist_client.get("/patients/search?q=zzzzzz")
        assert miss_resp.status_code == 200
        assert "No patients match" in miss_resp.text, \
            "a query with no matches should render the neutral no-match copy"

        assistant_typo_resp = assistant_client.get("/patients/search?q=rosi")
        assert assistant_typo_resp.status_code == 200
        assert "mario rossi" in assistant_typo_resp.text, \
            "assistant (holds read_notes) should also reach the search fragment"

        # 4. both list and detail as admin blocked (search fragment too)
        admin_search_resp = admin_client.get("/patients/search?q=rosi")
        assert admin_search_resp.status_code == 403, \
            "admin should get a bare 403 from the search fragment, not a redirect"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python patients_routes_selftest.py --selftest")


if __name__ == "__main__":
    main()
