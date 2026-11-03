"""P29 staff fixes in Chromium on a disposable synthetic clinic: the cancel confirmation (R-009), Show when the
filters are not applied (R-010), show/hide on the password fields (R-002), the patients column that flips
(F-005) and the refused booking that keeps its entries (R-006/R-007). No real account, model or service; the
loopback server and the database die with the run.

Run: .venv/bin/python e2e_p29_staff.py
"""
import tempfile
import threading
from datetime import date, timedelta
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from werkzeug.security import generate_password_hash
from werkzeug.serving import WSGIRequestHandler, make_server

import appointments
import availability
import demo_seed
import storage
import web_session
from app import create_app
from app import db as app_db


class Quiet(WSGIRequestHandler):
    def log(self, *args, **kwargs):
        pass


def main():
    with tempfile.TemporaryDirectory(prefix="clinic-p29-staff-") as tmp:
        app_db.DB_PATH = str(Path(tmp) / "clinic.sqlite")
        app_db.CHROMA_PATH = str(Path(tmp) / "chroma")
        conn = storage.init_db(app_db.DB_PATH)
        availability.seed_fixture_hours(conn)
        for who in ("dentist", "p29_dentist"):
            for weekday in range(7):
                conn.execute("INSERT OR IGNORE INTO dentist_schedule (dentist, weekday, starts, ends)"
                             " VALUES (?, ?, '08:00', '19:00')", (who, weekday))
        conn.commit()
        demo_seed.seed(conn, anchor=date.today().isoformat())
        conn.execute("INSERT OR IGNORE INTO users (username, password_hash, role, active) VALUES"
                     " ('p29_dentist', ?, 'dentist', 1)", (generate_password_hash("x"),))
        day = (date.today() + timedelta(days=7)).isoformat()
        cf = conn.execute("SELECT codice_fiscale FROM patients ORDER BY patient_name LIMIT 1").fetchone()[0]
        name = conn.execute("SELECT patient_name FROM patients WHERE codice_fiscale = ?", (cf,)).fetchone()[0]
        appointments.book(conn, cf, "p29_dentist", f"{day}T11:00", 30)
        conn.commit()
        token = web_session.create_session(conn, "p29_dentist", "dentist")
        conn.close()
        app = create_app()
        app.config["TESTING"] = True
        server = make_server("127.0.0.1", 0, app, request_handler=Quiet)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        checks = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                for width in (1440, 390):
                    context = browser.new_context(viewport={"width": width, "height": 900})
                    context.add_cookies([{"name": web_session.COOKIE_NAME, "value": token, "url": base}])
                    page = context.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))

                    # R-009: Cancel opens a dialog naming the appointment; Yes is held 5 s unless "I am sure"
                    page.goto(f"{base}/appointments?view=day&day={day}")
                    page.locator(f"form[data-confirm-cancel^='{name}'] button[type=submit]").first.click()
                    dialog = page.locator("#cancel-dialog")
                    expect(dialog).to_be_visible()
                    expect(dialog.locator("#cancel-dialog-what")).to_contain_text(name)
                    expect(dialog.locator("#cancel-dialog-what")).to_contain_text("11:00–11:30")
                    yes = dialog.locator("[data-cancel-yes]")
                    expect(yes).to_be_disabled()
                    expect(yes).to_contain_text("(5)")
                    page.wait_for_timeout(1200)
                    expect(yes).to_be_disabled()
                    # Keep it closes and nothing is cancelled
                    dialog.locator("[data-cancel-keep]").click()
                    expect(dialog).to_be_hidden()
                    # waiting out the timer only enables the button, it never cancels by itself
                    page.locator(f"form[data-confirm-cancel^='{name}'] button[type=submit]").first.click()
                    page.wait_for_timeout(5600)
                    expect(yes).to_be_enabled()
                    expect(dialog).to_be_visible()
                    dialog.locator("[data-cancel-keep]").click()
                    page.reload()
                    expect(page.locator(f"form[data-confirm-cancel^='{name}']")).to_have_count(1)
                    # "I am sure" enables it at once
                    page.locator(f"form[data-confirm-cancel^='{name}'] button[type=submit]").first.click()
                    dialog.locator("[data-cancel-sure]").check()
                    expect(yes).to_be_enabled()
                    if width == 390:
                        yes.click()
                        expect(page.locator(".app-flash[data-flash=removed]")).to_contain_text("Appointment cancelled")
                    else:
                        dialog.locator("[data-cancel-keep]").click()
                    checks.append(f"R-009 cancel dialog {width}")

                    # R-010: Show lights up while the filters differ from the page, and goes back when they match
                    page.goto(f"{base}/appointments?view=day&day={day}")
                    show = page.locator("[data-apply-button]")
                    assert "btn-primary" not in (show.get_attribute("class") or ""), "R-010: lit on load"
                    page.select_option("#f-status", "all")
                    assert "btn-primary" in show.get_attribute("class"), "R-010: not lit after a change"
                    expect(page.locator("[data-apply-hint]")).to_be_visible()
                    page.select_option("#f-status", "booked")
                    assert "btn-primary" not in show.get_attribute("class"), "R-010: still lit after undoing it"
                    checks.append(f"R-010 show highlight {width}")

                    # R-006 / R-007: a clash comes back as a red error with the entries kept
                    if width == 1440:
                        page.goto(f"{base}/appointments?view=day&day={day}&book=1")
                        page.select_option("#cf", cf)
                        page.fill("#bdate", day)
                        page.fill("#btime", "11:00")
                        page.fill("#bnote", "kept note")
                        page.click("#book button[type=submit]")
                        expect(page.locator(".app-flash[data-flash='not done']")).to_be_visible()
                        expect(page.locator("#book")).to_have_attribute("open", "")
                        expect(page.locator("#bnote")).to_have_value("kept note")
                        expect(page.locator("#btime")).to_have_value("11:00")
                        checks.append("R-006/R-007 refused booking kept")

                    # R-002: each password field shows and hides on its own
                    page.goto(base + "/change-password")
                    for field in ("current", "password", "confirm"):
                        page.fill(f"#{field}", "secret-" + field)
                        toggle = page.locator(f"[data-pw-toggle][aria-controls={field}]")
                        toggle.click()
                        expect(page.locator(f"#{field}")).to_have_attribute("type", "text")
                        expect(toggle).to_have_attribute("aria-pressed", "true")
                        toggle.click()
                        expect(page.locator(f"#{field}")).to_have_attribute("type", "password")
                    checks.append(f"R-002 password toggles {width}")
                    # NEW-02: the checklist ticks rule by rule as the new password grows
                    page.fill("#password", "Abc")
                    expect(page.locator("[data-rule=upper]")).to_have_class("is-met")
                    expect(page.locator("[data-rule=digit]")).not_to_have_class("is-met")
                    page.fill("#password", "Abcdefg1!")
                    expect(page.locator("[data-pw-rules] li.is-met")).to_have_count(5)
                    checks.append(f"NEW-02 checklist {width}")

                    # F-005: clicking the active column reverses it
                    page.goto(base + "/patients?sort=name")
                    first = page.locator("#patients-rows tr").first.get_attribute("data-name")
                    if width == 1440:
                        page.click("th[aria-sort] a")
                        expect(page.locator("th[aria-sort=descending]")).to_have_count(1)
                    else:
                        # phones show the rows as cards without the header row; the order still follows ?dir=
                        page.goto(base + "/patients?sort=name&dir=desc")
                    assert page.locator("#patients-rows tr").first.get_attribute("data-name") != first, \
                        "F-005: the order did not flip"
                    checks.append(f"F-005 header flips {width}")

                    width_px = page.evaluate("document.documentElement.scrollWidth")
                    assert width_px <= width, f"sideways scroll at {width}: {width_px}"
                    assert not errors, errors
                    context.close()
                browser.close()
        finally:
            server.shutdown()
        print("PASS:", "; ".join(checks))


if __name__ == "__main__":
    main()
