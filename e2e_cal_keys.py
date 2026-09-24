"""P03 T3: the month calendar is reachable and usable with the keyboard alone.

The fast suite asserts the markup and the counts; a11y_audit checks contrast and
landmarks. Neither one tabs. A grid of links that cannot be focused, or that
focuses with no visible ring, passes both and is unusable without a mouse - so
this is measured in a real browser or not at all.

ZZK fixtures, deleted in a finally. The namespace keeps it clear of
e2e_chat_walk's ZZE, e2e_intake_walk's ZZI and flow_appt's ZZV.
"""
import sys
from datetime import date, timedelta

from playwright.sync_api import sync_playwright
from werkzeug.security import generate_password_hash

import patient_id
import storage

STAFF, DB = "http://127.0.0.1:5000", "db/clinic.sqlite"
CF, NAME = "ZZKQ800101010108", "Keyboard Fixture"
USER, PASS = "zzk_keys_dentist", "zzk_keys_pass_1"
RESULTS = []


def check(name, ok, note=""):
    RESULTS.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + (f"  - {note}" if note else ""))


def main():
    conn = storage.connect(DB)
    day = (date.today() + timedelta(days=3)).isoformat()
    month = day[:7]
    try:
        pid = patient_id.seed_patient(conn, CF, NAME, None)
        conn.execute("INSERT INTO users (username, password_hash, role, active) VALUES (?,?,?,1)",
                     (USER, generate_password_hash(PASS), "dentist"))
        conn.commit()
        # stored as an INSTANT, the way book() writes one (P52). a naive row
        # here is an unmigrated row and the read path refuses it, which is the
        # correct behaviour and would fail this tool for the wrong reason.
        import clinic_time as _ct
        _ts = _ct.stamp()
        conn.execute(
            "INSERT INTO appointments (patient_id, dentist, starts_at, minutes, status,"
            " created_at, updated_at) VALUES (?,?,?,30,'booked',?,?)",
            (pid, USER, _ct.to_utc_text(_ct.parse(f"{day}T10:00:00")), _ts, _ts))
        conn.commit()

        with sync_playwright() as pw:
            b = pw.chromium.launch()
            s = b.new_context(viewport={"width": 1440, "height": 900}).new_page()
            s.goto(STAFF + "/login")
            s.fill('input[name="username"]', USER)
            s.fill('input[name="password"]', PASS)
            s.click('button[type="submit"]')
            s.wait_for_load_state("networkidle")
            s.goto(f"{STAFF}/appointments?day={day}&month={month}")
            s.wait_for_selector(".appt-cal")

            days = s.locator(".appt-cal-day")
            check("1 the month grid is drawn", days.count() >= 28, f"{days.count()} cells")

            # every cell is a real link with an href, not a div with a click
            # handler - the difference is invisible until you try to tab to it
            hrefs = days.evaluate_all("els => els.map(e => e.getAttribute('href'))")
            check("2 every day cell is a link with an href", all(hrefs) and len(hrefs) == days.count())

            # focus the first day, then walk the week with Tab alone
            first = days.first
            first.focus()
            focused = s.evaluate("document.activeElement.className")
            check("3 a day link takes focus", "appt-cal-day" in focused, focused)

            ring = s.evaluate("""() => {
                const el = document.activeElement;
                const cs = getComputedStyle(el);
                return {shadow: cs.boxShadow, outline: cs.outlineStyle + ' ' + cs.outlineWidth};
            }""")
            visible = (ring["shadow"] and ring["shadow"] != "none") or \
                      (ring["outline"] and "none" not in ring["outline"])
            check("4 the focused day shows a visible ring", visible, ring)

            seen = []
            for _ in range(7):
                s.keyboard.press("Tab")
                cls = s.evaluate("document.activeElement.className")
                href = s.evaluate("document.activeElement.getAttribute('href')")
                if "appt-cal-day" in (cls or ""):
                    seen.append(href)
            check("5 Tab walks along the week", len(seen) >= 6, f"{len(seen)} day links reached")

            # the booked day announces its count to a screen reader, and Enter
            # on a focused day actually navigates
            label = s.get_attribute(f'.appt-cal-day[href*="day={day}"]', "aria-label")
            check("6 the booked day states its count", label and "1 booked" in label, label)

            s.focus(f'.appt-cal-day[href*="day={day}"]')
            s.keyboard.press("Enter")
            s.wait_for_load_state("networkidle")
            check("7 Enter on a focused day opens that day", f"day={day}" in s.url, s.url)

            selected = s.get_attribute(f'.appt-cal-day[href*="day={day}"]', "aria-current")
            check("8 and the day is marked current once selected", selected == "date", selected)

            # the month arrows are links too - a keyboard user has to be able
            # to leave the month, not only move inside it
            nav = s.locator('.appt-cal-nav a')
            check("9 prev/today/next are keyboard-reachable links", nav.count() == 3,
                  f"{nav.count()} nav links")
            b.close()
    finally:
        conn.execute("DELETE FROM appointments WHERE patient_id = ?", (pid,))
        conn.execute("DELETE FROM patients WHERE codice_fiscale = ?", (CF,))
        conn.execute("DELETE FROM sessions WHERE username = ?", (USER,))
        conn.execute("DELETE FROM users WHERE username = ?", (USER,))
        conn.commit()
        import auth
        auth.purge_audit(conn, "username = ? OR target LIKE ? OR target = ?",
                         (USER, f"%{CF}%", pid), "cal_keys", "fixture cleanup")
        left = conn.execute(
            "SELECT (SELECT COUNT(*) FROM patients WHERE codice_fiscale=?)"
            " + (SELECT COUNT(*) FROM users WHERE username=?)"
            " + (SELECT COUNT(*) FROM appointments WHERE patient_id=?)",
            (CF, USER, pid)).fetchone()[0]
        print("leftover rows:", left)
    print(f"{sum(RESULTS)}/{len(RESULTS)} keyboard checks passed")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
