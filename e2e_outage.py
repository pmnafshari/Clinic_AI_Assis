"""P29 U-020: a save while the clinic app is not answering. Fill New patient, stop the server, press Save: the page
stays with what was typed and says nothing was sent - no white page, no row written. Bring the server back, press Try
again: exactly one patient is saved. A search typed while the server is down says the same. Chromium and WebKit (the
macOS app's engine), on a throwaway clinic of its own. usage: e2e_outage.py [--headed]"""
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright
from werkzeug.security import generate_password_hash
from werkzeug.serving import WSGIRequestHandler, make_server

import storage
import web_session

CF = "ZZOU800101010101"


class Quiet(WSGIRequestHandler):
    def log(self, *a, **k):
        pass


def serve(app, port):
    server = make_server("127.0.0.1", port, app, request_handler=Quiet, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def stop(server):
    server.shutdown()
    server.server_close()


def count(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM patients WHERE codice_fiscale = ?", (CF,)).fetchone()[0]
    finally:
        conn.close()


def walk(p, engine, app, db, token, headed):
    server = serve(app, 0)
    port = server.server_port
    base = f"http://127.0.0.1:{port}"
    browser = getattr(p, engine).launch(headless=not headed)
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    ctx.add_cookies([{"name": web_session.COOKIE_NAME, "value": token, "url": base}])
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(base + "/patients/new")
    page.fill("#patient_name", "Olga Outage")
    page.fill("#codice_fiscale", CF)
    page.fill("#phone", "3401110000")
    page.select_option("#document", index=1)
    stop(server)

    page.locator("form button[type=submit]").last.click()
    note = page.locator("[data-offline]")
    note.wait_for(state="visible", timeout=10000)
    assert "Nothing was sent" in note.inner_text(), f"{engine}: the outage message is not said"
    assert page.url.endswith("/patients/new"), f"{engine}: the page left - {page.url}"
    assert page.input_value("#patient_name") == "Olga Outage", f"{engine}: what was typed is lost"
    assert count(db) == 0, f"{engine}: a row was written while the server was down"

    # the search box's htmx request says the same, and OK closes it
    page.locator("[data-offline] button", has_text="OK").click()
    page.locator(".app-search input[name=q]").press_sequentially("Olg", delay=50)
    page.locator("[data-offline]").wait_for(state="visible", timeout=10000)
    page.locator("[data-offline] button", has_text="OK").click()
    assert page.locator("[data-offline]").count() == 0, f"{engine}: OK does not close the message"

    # back again: Try again saves once
    page.locator("form button[type=submit]").last.click()
    page.locator("[data-offline-retry]").wait_for(state="visible", timeout=10000)
    server = serve(app, port)
    try:
        with page.expect_navigation(timeout=15000):
            page.locator("[data-offline-retry]").click()
        assert count(db) == 1, f"{engine}: Try again saved {count(db)} rows"
        page.reload()
        assert count(db) == 1, f"{engine}: a reload saved again"
        assert not errors, f"{engine}: script errors {errors}"
    finally:
        stop(server)
        browser.close()
    print(f"PASS {engine}: no white page, typing kept, nothing sent while down; Try again saved exactly once")


def main():
    headed = "--headed" in sys.argv
    with tempfile.TemporaryDirectory() as tmp:
        import app.db as app_db
        from app import create_app
        db = str(Path(tmp) / "outage.sqlite")
        app_db.DB_PATH = db
        app_db.CHROMA_PATH = str(Path(tmp) / "chroma")
        conn = storage.init_db(db)
        conn.execute("INSERT INTO users (username, password_hash, role, active) VALUES ('ou_desk', ?, 'assistant', 1)",
                     (generate_password_hash("x"),))
        conn.commit()
        app = create_app()
        with sync_playwright() as p:
            for engine in ("chromium", "webkit"):
                conn.execute("DELETE FROM patients WHERE codice_fiscale = ?", (CF,))
                conn.commit()
                token = web_session.create_session(conn, "ou_desk", "assistant")
                walk(p, engine, app, db, token, headed)
        conn.close()


if __name__ == "__main__":
    main()
