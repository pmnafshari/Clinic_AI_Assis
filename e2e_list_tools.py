"""Staff list tools in Chromium: Filter, Sort, column headers, search and the top bar's menus on a disposable
synthetic clinic. No real account, model or service; the loopback server and the database die with the run.

Run: .venv/bin/python e2e_list_tools.py
"""
import re
import tempfile
import threading
from datetime import date
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from werkzeug.security import generate_password_hash
from werkzeug.serving import WSGIRequestHandler, make_server

import availability
import demo_billing
import demo_inventory
import demo_seed
import storage
import web_session
from app import create_app
from app import db as app_db


class Quiet(WSGIRequestHandler):
    def log(self, *args, **kwargs):
        pass


def shown(page, selector):
    """data-name of the rows that are showing, in order."""
    return page.eval_on_selector_all(selector, "rows => rows.filter(r => !r.hidden).map(r => r.dataset.name)")


def main():
    with tempfile.TemporaryDirectory(prefix="clinic-list-tools-") as tmp:
        app_db.DB_PATH = str(Path(tmp) / "clinic.sqlite")
        app_db.CHROMA_PATH = str(Path(tmp) / "chroma")
        conn = storage.init_db(app_db.DB_PATH)
        availability.seed_fixture_hours(conn)
        for weekday in range(7):
            conn.execute("INSERT OR IGNORE INTO dentist_schedule (dentist, weekday, starts, ends) VALUES ('dentist', ?, '08:00', '19:00')", (weekday,))
        conn.commit()
        demo_seed.seed(conn, anchor=date.today().isoformat())
        demo_billing.apply(conn)
        demo_inventory.apply(conn)
        conn.execute("INSERT OR IGNORE INTO users (username, password_hash, role, active) VALUES ('lt_dentist', ?, 'dentist', 1)",
                     (generate_password_hash("x"),))
        conn.commit()
        token = web_session.create_session(conn, "lt_dentist", "dentist")
        conn.close()
        app = create_app()
        app.config["TESTING"] = True
        server = make_server("127.0.0.1", 0, app, request_handler=Quiet)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                context = browser.new_context(viewport={"width": 1440, "height": 900})
                context.add_cookies([{"name": web_session.COOKIE_NAME, "value": token, "url": base}])
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))

                # patients: tick a filter, the rows narrow, a chip appears and removing it brings them back. P29 U-004:
                # this list asks the server (pages of 100), so each choice reloads the page with it in the address
                page.goto(base + "/patients")
                everyone = shown(page, "#patients-rows tr")
                assert len(everyone) >= 5, everyone
                tools = page.locator("[data-for=patients-rows]")
                tools.locator(".dropdown").nth(0).locator("[data-bs-toggle=dropdown]").click()
                tools.locator(".sx-opt:has-text('No phone') input").click()
                expect(page).to_have_url(re.compile(r"phone=no"))
                page.wait_for_load_state()
                narrowed = shown(page, "#patients-rows tr")
                assert 0 < len(narrowed) < len(everyone), narrowed
                expect(page.locator(".sx-chip")).to_have_count(1)
                expect(page.locator(".sx-list-status")).to_contain_text(f"{len(narrowed)} of {len(everyone)} patients shown")
                page.keyboard.press("Escape")
                # the shaded rows count only the rows that show
                first = page.locator("#patients-rows tr:not([hidden])").nth(0)
                second = page.locator("#patients-rows tr:not([hidden])").nth(1)
                assert first.locator("td").first.evaluate("e => getComputedStyle(e).backgroundColor") != \
                    second.locator("td").first.evaluate("e => getComputedStyle(e).backgroundColor"), "rows are not told apart"
                tools.locator(".dropdown").nth(1).locator("[data-bs-toggle=dropdown]").click()
                tools.locator(".sx-menu-item:has-text('Last visit (oldest)')").click()
                expect(page).to_have_url(re.compile(r"sort=last&dir=asc|dir=asc&sort=last"))
                page.wait_for_load_state()
                oldest = page.eval_on_selector_all("#patients-rows tr:not([hidden])", "rows => rows.map(r => r.dataset.last)")
                dated = [d for d in oldest if d != "0000-00-00"]
                # the server keeps a patient with no visit at the end whichever way the column runs (F-005)
                assert dated == sorted(dated) and oldest == dated + ["0000-00-00"] * (len(oldest) - len(dated)), oldest
                expect(page.locator(".sx-chip")).to_have_count(1)
                page.locator(".sx-chip").click()
                expect(page).not_to_have_url(re.compile(r"phone=no"))
                page.wait_for_load_state()
                assert shown(page, "#patients-rows tr") and len(shown(page, "#patients-rows tr")) == len(everyone)

                # invoices: the Amount column header sorts, a second click flips it
                page.goto(base + "/billing")
                header = page.locator("#bill-rows-draft").locator("xpath=ancestor::table").locator("th[data-sort=amount] .sx-th")
                cents = lambda: [int(v) for v in page.eval_on_selector_all("#bill-rows-draft tr", "rows => rows.map(r => r.dataset.amount)")]
                header.click()
                assert cents() == sorted(cents(), reverse=True), cents()       # a first click picks the column, the highest first
                header.click()
                assert cents() == sorted(cents()), cents()                      # a second click flips it
                tools = page.locator("[data-for=bill-rows-draft]")
                tools.locator(".dropdown").nth(1).locator("[data-bs-toggle=dropdown]").click()
                tools.locator(".sx-menu-item:has-text('Amount (highest first)')").click()
                assert cents() == sorted(cents(), reverse=True), cents()

                # billing is one box: Drafts shows only the drafts, All brings every group back
                page.locator("[data-bill-show] [data-show=draft]").click()
                expect(page.locator("[data-bill-panel=draft]")).to_be_visible()
                expect(page.locator("[data-bill-panel=owed]")).to_be_hidden()
                expect(page.locator("[data-bill-panel=unknown]")).to_be_hidden()
                page.locator("[data-bill-show] [data-show=all]").click()
                expect(page.locator("[data-bill-panel=owed]")).to_be_visible()

                # stock: the Search button finds an item, the line below says how many show
                page.goto(base + "/stock")
                total = len(shown(page, "#stock-rows tr"))
                page.fill("#stock-rows-search", "guanti")
                page.locator("[data-for=stock-rows] form button[type=submit]").click()
                found = shown(page, "#stock-rows tr")
                assert found and len(found) < total and all("guanti" in n.lower() for n in found), found
                page.fill("#stock-rows-search", "zzz-nothing")
                expect(page.locator(".sx-list-empty")).to_be_visible()

                # the top bar: the Search button opens the patients list on what was typed, the bell and the
                # account menu open and close, and the clock ticks the minute the server sent
                page.goto(base + "/")
                page.fill(".app-search input", "rizzo")
                page.locator(".app-search-go").click()
                page.wait_for_url(base + "/patients?q=rizzo")
                page.locator(".app-bell").click()
                expect(page.locator(".app-notif")).to_be_visible()
                assert page.locator(".app-notif-item").count() == int(page.locator(".app-bell-count").inner_text())
                page.keyboard.press("Escape")
                page.locator(".app-account").click()
                expect(page.locator(".app-account-menu")).to_contain_text("Sign out")
                page.keyboard.press("Escape")
                assert page.locator("[data-clock-time]").inner_text() == page.locator("[data-clock]").evaluate(
                    "e => String(Math.floor(e.dataset.minutes / 60)).padStart(2, '0') + ':' + String(e.dataset.minutes % 60).padStart(2, '0')")

                # the record: consent wording is folded behind Read out and opens under its own row
                page.goto(base + "/patients")
                page.goto(base + page.locator("#patients-rows a.btn").first.get_attribute("href"))
                # NEW-07: what the notes suggest for the dental chart opens by itself for a dentist; left for later here
                popup = page.locator("#chartSuggest")
                if popup.count():
                    popup.wait_for(state="visible", timeout=5000)
                    popup.locator("button[data-bs-dismiss=modal]").first.click()
                    popup.wait_for(state="hidden", timeout=5000)
                page.get_by_role("tab", name="Portal & consent").click()
                wording = page.locator("#wording-recording")
                expect(wording).to_be_hidden()
                read_out = page.locator("[data-read-out][aria-controls=wording-recording]")
                read_out.click()
                expect(wording).to_be_visible()
                expect(read_out).to_have_attribute("aria-expanded", "true")

                # a phone: nothing runs off the side, and the top bar keeps its Search button
                phone = browser.new_context(viewport={"width": 390, "height": 844})
                phone.add_cookies([{"name": web_session.COOKIE_NAME, "value": token, "url": base}])
                small = phone.new_page()
                for path in ("/", "/patients", "/billing", "/stock", page.url.replace(base, "")):
                    small.goto(base + path)
                    assert small.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"{path} overflows at 390px"
                expect(small.locator(".app-search-go")).to_be_visible()
                assert not errors, errors
                browser.close()
        finally:
            server.shutdown()
    print("PASS: list tools, column headers, search, top bar menus and the phone layout")


if __name__ == "__main__":
    main()
