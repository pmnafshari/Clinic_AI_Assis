import re
import sqlite3
import sys
import tempfile
from pathlib import Path

from werkzeug.security import generate_password_hash

import app.db as app_db
import web_session
from app import create_app

CLINICAL_STRINGS = ("Ask a question", "Add a note", "Edit a record", "Run a command")


def _seed_user(db_path, username, password, role, must_change=0):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO users (username, password_hash, role, active, must_change_password)"
        " VALUES (?, ?, ?, 1, ?)",
        (username, generate_password_hash(password), role, must_change),
    )
    conn.commit()
    conn.close()


def _csrf_from(html):
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return match.group(1)


def _login(app, username, password):
    client = app.test_client()
    csrf = _csrf_from(client.get("/login").text)
    resp = client.post(
        "/login", data={"username": username, "password": password, "csrf_token": csrf}
    )
    assert resp.status_code == 302, f"login for {username} should redirect"
    return client


def _user(db_path, username):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    return row


def _denied_count(db_path):
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM audit_log WHERE allowed = 0").fetchone()[0]
    conn.close()
    return count


def _session_count(db_path, username):
    conn = sqlite3.connect(db_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE username = ?", (username,)
    ).fetchone()[0]
    conn.close()
    return count


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = str(tmp_path / "clinic.sqlite")

        app_db.DB_PATH = db_path
        app_db.CHROMA_PATH = str(tmp_path / "chroma")

        app = create_app()
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False

        _seed_user(db_path, "aadmin", "goodpass", "admin")
        _seed_user(db_path, "drossi", "goodpass", "dentist")
        _seed_user(db_path, "aassist", "goodpass", "assistant")

        admin = _login(app, "aadmin", "goodpass")

        # ---- SC1: create an account and assign it a role -------------------
        resp = admin.post(
            "/admin/users",
            data={"username": "newstaff", "role": "assistant", "password": "temp-pass"},
        )
        assert resp.status_code == 302, "SC1: create should redirect back to the list"
        row = _user(db_path, "newstaff")
        assert row is not None, "SC1: account was not created"
        assert row["role"] == "assistant", f"SC1: wrong role {row['role']}"
        assert row["password_hash"].startswith("scrypt:"), "SC1: password not scrypt-hashed"
        assert row["must_change_password"] == 1, \
            "SC1: a created account must be forced to change its password"

        # ---- SC2: disable an account, and its sessions die with it ---------
        live = web_session.create_session(sqlite3.connect(db_path), "newstaff", "assistant")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO sessions (token_hash, username, role, created_at, last_seen_at)"
            " SELECT token_hash, username, role, created_at, last_seen_at FROM sessions"
            " WHERE username = 'newstaff' LIMIT 0"
        )
        conn.commit()
        conn.close()
        assert _session_count(db_path, "newstaff") >= 1, "SC2: seed a live session first"

        resp = admin.post("/admin/users/newstaff", data={"action": "active", "active": "0"})
        assert resp.status_code == 200, "SC2: disable should return the swapped row"
        assert _user(db_path, "newstaff")["active"] == 0, "SC2: account still active"
        assert _session_count(db_path, "newstaff") == 0, \
            "SC2: disabling must destroy the user's live sessions"

        # the row count is not the guarantee - what matters is that the user
        # holding that cookie is actually ejected on their next request
        victim = app.test_client()
        victim.set_cookie("session_token", live)
        resp = victim.get("/")
        assert resp.status_code == 302 and "/login" in resp.headers["Location"], \
            f"SC2: a disabled user's live cookie must be ejected, got {resp.status_code}"

        # ---- SC3: change an account's role ---------------------------------
        admin.post("/admin/users/newstaff", data={"action": "active", "active": "1"})
        resp = admin.post(
            "/admin/users/newstaff", data={"action": "role", "new_role": "dentist"}
        )
        assert resp.status_code == 200, "SC3: role change should return the swapped row"
        assert _user(db_path, "newstaff")["role"] == "dentist", "SC3: role not changed"

        # ---- enable via the route, asserted (not just used as setup) -------
        resp = admin.post("/admin/users/newstaff", data={"action": "active", "active": "1"})
        assert resp.status_code == 200, "enable should return the swapped row"
        assert _user(db_path, "newstaff")["active"] == 1, "enable did not reactivate"

        # ---- lockout: the third dispatch branch, and its confirm fragment --
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE users SET failed_attempts = 5, locked_until = ? WHERE username = ?",
            ("2099-01-01T00:00:00", "newstaff"),
        )
        conn.commit()
        conn.close()

        resp = admin.get("/admin/users/newstaff/confirm?action=lockout")
        assert resp.status_code == 200, "lockout confirm fragment should render"
        assert "Clear lockout" in resp.text, "lockout modal should name the action"

        resp = admin.post("/admin/users/newstaff", data={"action": "lockout"})
        assert resp.status_code == 200, "clear lockout should return the swapped row"
        row = _user(db_path, "newstaff")
        assert row["failed_attempts"] == 0 and row["locked_until"] is None, \
            "clear lockout did not reset the counter and timestamp"

        # a locked row must render the Locked badge and the clearing control
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE users SET failed_attempts = 3, locked_until = ? WHERE username = ?",
            ("2099-01-01T00:00:00", "newstaff"),
        )
        conn.commit()
        conn.close()
        listing = admin.get("/admin/users").text
        assert "Locked" in listing, "a locked account must show the Locked badge"
        assert "Clear lockout" in listing, "a locked account must offer Clear lockout"
        admin.post("/admin/users/newstaff", data={"action": "lockout"})

        # ---- the confirm fragment rejects a bad action and a bad role ------
        assert admin.get("/admin/users/newstaff/confirm?action=wat").status_code == 400, \
            "an unknown confirm action should be refused"
        assert admin.get(
            "/admin/users/newstaff/confirm?action=role&new_role=wizard"
        ).status_code == 400, "an invalid role should be refused at the fragment"
        assert admin.post(
            "/admin/users/newstaff", data={"action": "wat"}
        ).status_code == 400, "an unknown apply action should be refused"

        # ---- SC4: a non-admin cannot reach the admin screens ---------------
        for username in ("drossi", "aassist"):
            client = _login(app, username, "goodpass")

            before = _denied_count(db_path)
            resp = client.get("/admin/users")
            assert resp.status_code == 302, f"SC4: {username} should not get the list"
            assert _denied_count(db_path) == before + 1, \
                f"SC4: {username}'s denied page view must be audited"

            before = _denied_count(db_path)
            resp = client.get("/admin/users/newstaff/confirm?action=active")
            assert resp.status_code == 403, f"SC4: {username} should get 403 on the fragment"
            assert _denied_count(db_path) == before + 1, \
                f"SC4: {username}'s denied fragment must be audited"

            before = _denied_count(db_path)
            resp = client.post(
                "/admin/users/newstaff", data={"action": "role", "new_role": "admin"}
            )
            assert resp.status_code == 403, f"SC4: {username} should get 403 on the write"
            assert _user(db_path, "newstaff")["role"] == "dentist", \
                f"SC4: {username} changed a role anyway"
            assert _denied_count(db_path) == before + 1, \
                f"SC4: {username}'s denied write must be audited"

        # ---- SC5: the admin's view stays scoped ----------------------------
        resp = admin.get("/")
        assert resp.status_code == 302, "SC5: an admin must not render the dashboard"
        assert "/admin/users" in resp.headers["Location"], \
            f"SC5: admin should land on the staff screen, got {resp.headers['Location']}"

        body = admin.get("/", follow_redirects=True).text
        for needle in CLINICAL_STRINGS:
            assert needle not in body, f"SC5: clinical entry point '{needle}' visible to an admin"

        for path in ("/agent/edit", "/agent/command"):
            resp = admin.get(path)
            assert resp.status_code == 302, f"SC5: admin should be refused at {path}"

        # ---- guardrail: the self-edit block survives a forged POST ---------
        for payload in (
            {"action": "active", "active": "0"},
            {"action": "role", "new_role": "assistant"},
        ):
            before = _denied_count(db_path)
            resp = admin.post("/admin/users/aadmin", data=payload)
            assert resp.status_code == 200, "guardrail: a refused self-edit still renders a toast"
            me = _user(db_path, "aadmin")
            assert me["active"] == 1, f"guardrail: admin disabled themselves via {payload}"
            assert me["role"] == "admin", f"guardrail: admin demoted themselves via {payload}"
            assert _denied_count(db_path) == before + 1, \
                f"guardrail: refused self-edit must be audited ({payload})"

        # the modal must refuse to open too, not just the write
        resp = admin.get("/admin/users/aadmin/confirm?action=active")
        assert resp.status_code == 403, "guardrail: self-edit confirm fragment must be refused"

        # ---- guardrail: at least one active admin always remains -----------
        conn = sqlite3.connect(db_path)
        admins = conn.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1"
        ).fetchone()[0]
        conn.close()
        assert admins >= 1, "guardrail: no active admin left"

        # ---- guardrail: duplicate username is refused ----------------------
        admin.post(
            "/admin/users",
            data={"username": "newstaff", "role": "admin", "password": "x"},
        )
        conn = sqlite3.connect(db_path)
        dupes = conn.execute(
            "SELECT COUNT(*) FROM users WHERE username = 'newstaff'"
        ).fetchone()[0]
        conn.close()
        assert dupes == 1, f"guardrail: duplicate username created {dupes} rows"

        # ---- forced first-login password change lifecycle ------------------
        admin.post(
            "/admin/users",
            data={"username": "fresh", "role": "assistant", "password": "temp-pass"},
        )
        fresh = _login(app, "fresh", "temp-pass")

        for path in ("/", "/qa"):
            resp = fresh.get(path)
            assert resp.status_code == 302, f"forced-change: {path} should redirect"
            assert "/change-password" in resp.headers["Location"], \
                f"forced-change: {path} should go to the change screen"

        # the interceptor must not trap the user on a screen they cannot leave
        assert fresh.get("/change-password").status_code == 200, \
            "forced-change: the change screen itself must be reachable"
        assert fresh.post("/logout").status_code == 302, \
            "forced-change: logout must stay reachable"

        fresh = _login(app, "fresh", "temp-pass")
        # AUTH-05 D-01: the current password is required on the forced path too,
        # so every post here carries it
        resp = fresh.post(
            "/change-password",
            data={"current": "temp-pass", "password": "aaa", "confirm": "bbb"},
        )
        # jinja escapes the apostrophe, so match on the part that survives
        assert "The two passwords" in resp.text, "forced-change: mismatch should be reported"
        assert _user(db_path, "fresh")["must_change_password"] == 1, \
            "forced-change: a failed attempt must not clear the flag"

        old_hash = _user(db_path, "fresh")["password_hash"]
        resp = fresh.post(
            "/change-password",
            data={"current": "temp-pass", "password": "newpass1", "confirm": "newpass1"},
        )
        assert resp.status_code == 302, "forced-change: success should redirect"
        after = _user(db_path, "fresh")
        assert after["must_change_password"] == 0, "forced-change: flag not cleared"
        assert after["password_hash"] != old_hash, "forced-change: password not changed"
        assert after["password_hash"].startswith("scrypt:"), "forced-change: hash not scrypt"

        conn = sqlite3.connect(db_path)
        changed = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'change_password' AND allowed = 1"
        ).fetchone()[0]
        conn.close()
        assert changed == 1, f"forced-change: expected 1 audit row, got {changed}"

        resp = fresh.get("/")
        assert "/change-password" not in resp.headers.get("Location", ""), \
            "forced-change: user should be released once the password is set"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python admin_routes_selftest.py --selftest")


if __name__ == "__main__":
    main()
