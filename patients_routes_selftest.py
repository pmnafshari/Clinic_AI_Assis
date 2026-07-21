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


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python patients_routes_selftest.py --selftest")


if __name__ == "__main__":
    main()
