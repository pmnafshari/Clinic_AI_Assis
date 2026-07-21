import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from werkzeug.security import generate_password_hash

import agent
import app.dashboard_routes as dashboard_routes
import app.db as app_db
from app import create_app
from web_session import SESSION_IDLE_MINUTES, _hash_token


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


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "clinic.sqlite")
        app_db.DB_PATH = db_path

        app = create_app()
        app.config["TESTING"] = True

        _seed_user(db_path, "drossi", "goodpass", "dentist")

        # 1. AUTH-01 login success reaches the dashboard showing username + role
        client_a = app.test_client()
        get_resp = client_a.get("/login")
        assert get_resp.status_code == 200, "1: GET /login should return 200"
        csrf_a = _csrf_from(get_resp.text)

        login_resp = client_a.post(
            "/login",
            data={"username": "drossi", "password": "goodpass", "csrf_token": csrf_a},
        )
        assert login_resp.status_code == 302, "1: successful login should redirect"

        dash_resp = client_a.get("/")
        assert dash_resp.status_code == 200, "1: dashboard should be reachable after login"
        assert b"drossi" in dash_resp.data and b"dentist" in dash_resp.data, \
            "1: dashboard should show the logged-in username and role"

        # 2. AUTH-02 cookie flags - session_token carries HttpOnly + SameSite=Strict
        set_cookie_headers = login_resp.headers.getlist("Set-Cookie")
        session_cookie = next(h for h in set_cookie_headers if h.startswith("session_token="))
        assert "HttpOnly" in session_cookie, "2: session_token cookie must be HttpOnly"
        assert "SameSite=Strict" in session_cookie, "2: session_token cookie must be SameSite=Strict"

        # 3. AUTH-02 persistence - a later request on the same client stays authenticated
        second_resp = client_a.get("/")
        assert second_resp.status_code == 200, "3: session should persist across requests"

        # 4. AUTH-01 generic failure - wrong password gives one generic message
        client_b = app.test_client()
        get_resp_b = client_b.get("/login")
        csrf_b = _csrf_from(get_resp_b.text)
        bad_resp = client_b.post(
            "/login",
            data={"username": "drossi", "password": "wrongpass", "csrf_token": csrf_b},
        )
        assert bad_resp.status_code == 200, "4: failed login should re-render the login page"
        assert b"invalid username or password" in bad_resp.data, \
            "4: failed login should show the generic error"
        assert client_b.get_cookie("session_token") is None, \
            "4: a failed login must not issue a session cookie"

        # 5. D-05 CSRF required - a login POST without csrf_token is rejected
        client_c = app.test_client()
        client_c.get("/login")
        no_csrf_resp = client_c.post(
            "/login", data={"username": "drossi", "password": "goodpass"}
        )
        assert no_csrf_resp.status_code == 400, "5: missing csrf_token should be rejected"
        assert client_c.get_cookie("session_token") is None, \
            "5: a rejected csrf login must not issue a session cookie"

        # 6. D-08 / Pitfall 1 - logged-out default-deny, unmatched URL stays a 404
        client_d = app.test_client()
        deny_resp = client_d.get("/")
        assert deny_resp.status_code == 302, "6: logged-out GET / should redirect"
        assert "/login" in deny_resp.headers["Location"], "6: redirect target should be /login"
        missing_resp = client_d.get("/no-such-page")
        assert missing_resp.status_code == 404, "6: unmatched URL should 404, not redirect"

        # 7. AUTH-02 idle expiry - a stale session redirects to login
        client_e = app.test_client()
        get_resp_e = client_e.get("/login")
        csrf_e = _csrf_from(get_resp_e.text)
        client_e.post(
            "/login",
            data={"username": "drossi", "password": "goodpass", "csrf_token": csrf_e},
        )
        raw_token = client_e.get_cookie("session_token").value
        past = datetime.now() - timedelta(minutes=SESSION_IDLE_MINUTES + 1)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?",
            (past.isoformat(), _hash_token(raw_token)),
        )
        conn.commit()
        conn.close()
        idle_resp = client_e.get("/")
        assert idle_resp.status_code == 302, "7: idle-expired session should redirect"
        assert "/login" in idle_resp.headers["Location"], "7: idle redirect target should be /login"

        # 8. AUTH-02 logout invalidation - the session row is gone, next request redirects
        client_f = app.test_client()
        get_resp_f = client_f.get("/login")
        csrf_f = _csrf_from(get_resp_f.text)
        client_f.post(
            "/login",
            data={"username": "drossi", "password": "goodpass", "csrf_token": csrf_f},
        )
        logout_raw_token = client_f.get_cookie("session_token").value
        dash_resp_f = client_f.get("/")
        csrf_f2 = _csrf_from(dash_resp_f.text)
        logout_resp = client_f.post("/logout", data={"csrf_token": csrf_f2})
        assert logout_resp.status_code == 302, "8: logout should redirect"
        assert "/login" in logout_resp.headers["Location"], "8: logout should redirect to /login"

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT 1 FROM sessions WHERE token_hash = ?", (_hash_token(logout_raw_token),)
        ).fetchone()
        conn.close()
        assert row is None, "8: destroy_session should delete the session row"

        after_logout_resp = client_f.get("/")
        assert after_logout_resp.status_code == 302, "8: a request after logout should redirect"
        assert "/login" in after_logout_resp.headers["Location"], \
            "8: post-logout redirect target should be /login"

        # 9. GUI-05/D-07 - dashboard shows only the acting user's own undo history
        _seed_user(db_path, "drossi2", "goodpass", "dentist")
        dashboard_routes.UNDO_LOG = str(Path(tmp) / "undo_log.jsonl")
        agent.write_undo_entry(
            {
                "ts": "2026-01-01T00:00:00",
                "tool": "update_field",
                "codice_fiscale": "RSSM800010150100",
                "target": "sqlite:patients.phone",
                "before": "111-1111",
                "username": "drossi",
            },
            dashboard_routes.UNDO_LOG,
        )
        agent.write_undo_entry(
            {
                "ts": "2026-01-01T00:00:01",
                "tool": "update_field",
                "codice_fiscale": "MRTLGU900010150100",
                "target": "sqlite:patients.phone",
                "before": "222-2222",
                "username": "drossi2",
            },
            dashboard_routes.UNDO_LOG,
        )

        client_g = app.test_client()
        get_resp_g = client_g.get("/login")
        csrf_g = _csrf_from(get_resp_g.text)
        client_g.post(
            "/login",
            data={"username": "drossi", "password": "goodpass", "csrf_token": csrf_g},
        )
        dash_resp_g = client_g.get("/")
        assert b"RSSM800010150100" in dash_resp_g.data, \
            "9: dashboard should show the acting user's own change"
        assert b"Undo change" in dash_resp_g.data, \
            "9: the most-recent row should carry an Undo change link"
        assert b"MRTLGU900010150100" not in dash_resp_g.data, \
            "9: another user's entry must not be shown"

        # 10. GUI-06 SC1 - no-CDN: authenticated pages fetch every asset
        # locally, no external http(s) reference in a <script>/<link> tag
        no_cdn_resp = client_a.get("/")
        assert not re.search(
            r'<(?:script|link)[^>]+(?:src|href)="https?://', no_cdn_resp.text, re.IGNORECASE
        ), "10: authenticated pages must not reference any external http(s) asset"

        # 11. D-05 - login opts out of the sidebar; an authenticated screen
        # keeps it
        login_page = app.test_client().get("/login")
        assert b"<aside" not in login_page.data, "11: login must render without the sidebar"
        dash_with_shell = client_a.get("/")
        assert b"<aside" in dash_with_shell.data, "11: an authenticated screen must render the sidebar"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python app_selftest.py --selftest")


if __name__ == "__main__":
    main()
