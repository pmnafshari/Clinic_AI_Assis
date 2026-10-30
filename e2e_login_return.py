"""Staff login navigation in Chromium, using only disposable synthetic storage.

Run: .venv/bin/python e2e_login_return.py
No existing clinic service or model is needed.
"""
import tempfile
import threading
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import WSGIRequestHandler, make_server

import app.db as app_db
from login_return_selftest import PASSWORD, TARGET, make_app


class QuietHandler(WSGIRequestHandler):
    def log(self, *args, **kwargs):
        pass


def login(page, user="return_dentist", password=PASSWORD):
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "login overflow"
    page.get_by_label("Username", exact=True).fill(user)
    page.keyboard.press("Tab")
    expect(page.get_by_label("Password", exact=True)).to_be_focused()
    page.get_by_label("Password", exact=True).fill(password)
    page.keyboard.press("Enter")


def walk(width):
    old_paths = app_db.DB_PATH, app_db.CHROMA_PATH
    with tempfile.TemporaryDirectory(prefix="clinic-p29-browser-") as root:
        server = None
        try:
            app = make_app(root)
            server = make_server("127.0.0.1", 0, app, request_handler=QuietHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            with sync_playwright() as p:
                browser = p.chromium.launch()
                context = browser.new_context(viewport={"width": width, "height": 900})
                # A regression must not navigate to the external redirect probe.
                context.route("**/*", lambda route: route.continue_() if
                              urlsplit(route.request.url).netloc == urlsplit(base).netloc else route.abort())
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(base + TARGET)
                expect(page.locator('input[name="next"]')).to_have_value(TARGET)
                login(page, password="wrong")
                expect(page.get_by_role("alert")).to_contain_text("invalid username or password")
                expect(page.locator('input[name="next"]')).to_have_value(TARGET)
                login(page)
                expect(page).to_have_url(base + TARGET)
                expect(page.get_by_role("heading", name="Files and documents")).to_be_visible()
                print("PASS real protected GET, failed login, query and inherited fragment")

                context.clear_cookies()
                page.goto("about:blank")
                page.goto(base + TARGET)
                other = context.new_page()
                other.goto(base + "/stock")
                login(page)
                expect(page).to_have_url(base + TARGET)
                login(other)
                expect(other).to_have_url(base + "/stock")
                other.close()
                print("PASS independent tabs retain their own destinations")

                for role in ("assistant", "admin"):
                    context.clear_cookies()
                    page.goto("about:blank")
                    page.goto(base + TARGET)
                    login(page, user=f"return_{role}")
                    home = "/admin/users" if role == "admin" else "/"
                    expect(page).to_have_url(base + home + "#page-2")
                    expect(page.get_by_text("Only a dentist can work with clinical documents.")).to_be_visible()
                print("PASS destination reauthorizes assistant and admin")

                context.clear_cookies()
                page.goto("about:blank")
                page.goto(base + TARGET)
                login(page, user="return_new")
                expect(page.get_by_role("heading", name="Set a new password")).to_be_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "password form overflow"
                expect(page.locator('input[name="next"]')).to_have_value(TARGET)
                page.get_by_label("Current password", exact=True).fill(PASSWORD)
                page.get_by_label("New password", exact=True).fill("new-synthetic-password")
                page.get_by_label("Confirm password", exact=True).fill("mismatch")
                page.get_by_role("button", name="Set password").click()
                expect(page.get_by_role("alert")).to_contain_text("don't match")
                expect(page.locator('input[name="next"]')).to_have_value(TARGET)
                page.get_by_label("Current password", exact=True).fill(PASSWORD)
                page.get_by_label("New password", exact=True).fill("new-synthetic-password")
                page.get_by_label("Confirm password", exact=True).fill("new-synthetic-password")
                page.get_by_role("button", name="Set password").click()
                expect(page.get_by_role("heading", name="Log in", exact=True)).to_be_visible()
                login(page, user="return_new", password="new-synthetic-password")
                expect(page).to_have_url(base + TARGET)
                print("PASS password change, failed confirmation and mandatory reauthentication")

                context.clear_cookies()
                page.goto("about:blank")
                page.goto(base + "/login?next=//example.invalid/")
                login(page)
                expect(page).to_have_url(base + "/")
                assert errors == [], errors
                print("PASS external redirect rejected; no browser script errors")
                browser.close()
        finally:
            if server:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()
            app_db.DB_PATH, app_db.CHROMA_PATH = old_paths
    print(f"PASS width {width}; keyboard login; temporary DB removed; own test server stopped")


if __name__ == "__main__":
    for width in (390, 1440):
        walk(width)
