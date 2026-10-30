"""Payment form navigation on a private test server with disposable storage."""
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

import app.db as app_db
from e2e_login_return import QuietHandler, login
from login_return_selftest import CF, make_app
from payment_intent_selftest import connect, invoice_fixture


def walk(width, screenshots=None):
    old_paths = app_db.DB_PATH, app_db.CHROMA_PATH
    with tempfile.TemporaryDirectory(prefix="clinic-p29-payment-browser-") as root:
        server = None
        try:
            app = make_app(root)
            conn = connect()
            inv = invoice_fixture(conn)
            conn.close()
            server = make_server("127.0.0.1", 0, app, request_handler=QuietHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            with sync_playwright() as p:
                browser = p.chromium.launch()
                context = browser.new_context(viewport={"width": width, "height": 900})
                context.route("**/*", lambda route: route.continue_() if
                              urlsplit(route.request.url).netloc == urlsplit(base).netloc else route.abort())
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(base + f"/patients/{CF}/billing")
                login(page, user="return_assistant")
                page.get_by_role("link", name="Start payment", exact=True).click()
                form_url = page.url
                token = page.locator('input[name="intent"]').input_value()
                assert token not in page.url and "?form=" in page.url
                page.reload()
                expect(page.locator('input[name="intent"]')).to_have_value(token)
                expect(page.get_by_label("Amount received (EUR)")).to_be_focused()
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "form overflow"
                if screenshots:
                    page.screenshot(path=str(screenshots / f"payment-form-{width}.png"), full_page=True)
                # A separate form from the same balance must go stale when this one is saved.
                other = context.new_page()
                other.goto(base + f"/billing/invoices/{inv}/payment/new")
                other_token = other.locator('input[name="intent"]').input_value()
                assert token != other_token
                page.get_by_label("Amount received (EUR)").fill("462.50")
                page.keyboard.press("Tab")
                expect(page.get_by_label("Method", exact=True)).to_be_focused()
                page.keyboard.press("Tab")
                expect(page.get_by_label("Receipt no. (optional)")).to_be_focused()
                page.keyboard.type("synthetic-receipt")
                page.keyboard.press("Tab")
                expect(page.get_by_role("button", name="Record payment", exact=True)).to_be_focused()
                page.keyboard.press("Enter")
                expect(page.get_by_role("heading", name="Payment recorded", exact=True)).to_be_visible()
                assert "?receipt=" in page.url and token not in page.url
                page.reload()
                expect(page.get_by_role("heading", name="Payment recorded", exact=True)).to_be_visible()
                page.go_back()
                # Going Back can land on the original GET or the billing page, never a POST.
                assert "ERR_CACHE_MISS" not in page.content()
                page.goto(form_url)
                expect(page.locator('input[name="amount"]')).to_have_count(0)
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "receipt overflow"
                if screenshots:
                    page.screenshot(path=str(screenshots / f"payment-receipt-{width}.png"), full_page=True)
                other.get_by_label("Amount received (EUR)").fill("462.50")
                other.get_by_role("button", name="Record payment", exact=True).click()
                expect(other.get_by_role("alert").first).to_contain_text("payments have changed")
                expect(other.locator('input[name="amount"]')).to_have_count(0)
                conn = connect()
                assert conn.execute("SELECT COUNT(*), SUM(amount_cents) FROM payments").fetchone()[:] == (1, 46250)
                conn.close()
                page.get_by_role("link", name="Start another payment", exact=True).click()
                assert page.locator('input[name="intent"]').input_value() != token
                page.get_by_label("Amount received (EUR)").fill("462.50")
                page.get_by_role("button", name="Record payment", exact=True).click()
                expect(page.get_by_role("heading", name="Payment recorded", exact=True)).to_be_visible()
                conn = connect()
                assert conn.execute("SELECT COUNT(*), SUM(amount_cents) FROM payments").fetchone()[:] == (2, 92500)
                assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='record_payment' AND allowed=1").fetchone()[0] == 2
                conn.close()
                assert not errors, errors
                browser.close()
        finally:
            if server:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()
            app_db.DB_PATH, app_db.CHROMA_PATH = old_paths
    print(f"PASS {width}px: keyboard, reload/Back, receipt, stale second tab, explicit new payment; no JS errors")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--screenshots", type=Path)
    args = parser.parse_args()
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)
    for width in (390, 1440):
        walk(width, args.screenshots)
