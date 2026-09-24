"""Gate G5, integration flow: patient request (41/42/45) -> staff dashboard queue (43)
-> staff confirm on /appointments (42/44) -> patient overview shows the booked time (45)
-> staff dashboard agenda shows it (41/43). ZZV fixtures, deleted in finally."""
import re
import sys
from datetime import date, timedelta

from playwright.sync_api import sync_playwright
from werkzeug.security import generate_password_hash

import patient_auth
import patient_id
import storage

STAFF, PATIENT, DB = "http://127.0.0.1:5000", "http://127.0.0.1:5001", "db/clinic.sqlite"
CF, NAME, NEW_PIN = "ZZVQ800101010108", "Ilaria Conti", "97531864"
USER, PASS = "zzv_flow_dentist", "zzv_flow_pass_1"
RESULTS = []


def check(name, ok, note=""):
    RESULTS.append(ok)
    print(("PASS " if ok else "FAIL ") + name + (f"  - {note}" if note else ""))


def main():
    conn = storage.connect(DB)
    # the next day the clinic is open, from tomorrow on: "tomorrow" was a Saturday on Fridays,
    # and the clinic is closed at weekends, so the confirm below was refused (found 2026-09-25)
    open_days = {r[0] for r in conn.execute("SELECT weekday FROM clinic_hours WHERE closed = 0")}
    closed = {r[0] for r in conn.execute("SELECT closure_date FROM clinic_closures")} if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'clinic_closures'").fetchone() else set()
    d = date.today() + timedelta(days=1)
    while d.weekday() not in open_days or d.isoformat() in closed:
        d += timedelta(days=1)
    day = d.isoformat()   # upcoming, and bookable
    try:
        pid = patient_id.seed_patient(conn, CF, NAME, None)
        conn.execute("INSERT INTO users (username, password_hash, role, active) VALUES (?,?,?,1)",
                     (USER, generate_password_hash(PASS), "dentist"))
        # P05: a dentist who is not rostered cannot be booked, so a throwaway
        # fixture dentist needs a roster or every confirm below is refused
        # before it reaches what this tool is actually testing. deleted in the
        # finally with the rest of the ZZV fixtures.
        for weekday in range(7):
            conn.execute("INSERT OR IGNORE INTO dentist_schedule (dentist, weekday, starts, ends)"
                         " VALUES (?, ?, '00:00', '23:59')", (USER, weekday))
        conn.commit()
        pin = patient_auth.issue_pin(CF, conn, "dentist", "dentist")
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            # --- patient requests
            p = b.new_context().new_page()
            p.goto(PATIENT + "/login")
            p.fill("#codice_fiscale", CF); p.fill("#pin", pin)
            p.click("form[action*='login'] button[type=submit]"); p.wait_for_load_state("networkidle")
            p.fill("#pin", NEW_PIN); p.fill("#confirm", NEW_PIN)
            p.click("form[action*='change-pin'] button[type=submit]"); p.wait_for_load_state("networkidle")
            p.goto(PATIENT + "/appointments")
            p.fill("#day", day); p.check("#period-afternoon"); p.fill("#reason", "zzv flow")
            p.click("form[action*='appointments/request'] button[type=submit]"); p.wait_for_load_state("networkidle")
            p.goto(PATIENT + "/")
            home = p.content()
            check("1 patient overview counts the new request", "da confermare" in home or "to confirm" in home)
            check("1b and never times it", "00:00" not in home)

            # --- staff sees it on the dashboard queue
            s = b.new_context(viewport={"width": 1440, "height": 900}).new_page()
            s.goto(STAFF + "/login")
            s.fill('input[name="username"]', USER); s.fill('input[name="password"]', PASS)
            s.click('button[type="submit"]'); s.wait_for_load_state("networkidle")
            dash = s.content()
            queue = dash.split('id="requests-title"', 1)[1].split("</section>", 1)[0] if 'id="requests-title"' in dash else ""
            check("2 staff dashboard queue shows the request", NAME in queue and day in queue and "afternoon" in queue)
            check("2b queue draws no time for it", not re.search(r"\b\d{2}:\d{2}\b", queue))

            # --- header search finds the patient (43 -> patients.search_fragment)
            s.locator(".app-search input").press_sequentially("Ilaria Con", delay=40)
            s.wait_for_selector("#app-search-results a", timeout=5000)
            check("3 header search finds the patient", NAME in s.inner_text("#app-search-results"))

            # --- staff confirms on /appointments (44 layout, 42 route)
            s.click(".app-cta"); s.wait_for_load_state("networkidle")
            check("4 New appointment lands on the booking form", s.url.endswith("/appointments#book")
                  and s.locator("#book").count() == 1, s.url)
            item = s.locator(".appt-item", has_text=NAME)
            item.locator("summary").click()
            item.locator("input[name=date]").fill(day)
            item.locator("input[name=time]").fill("16:30")
            item.locator("select[name=dentist]").select_option(USER)
            item.locator("form[action*='confirm'] button[type=submit]").click()
            s.wait_for_load_state("networkidle")
            row = conn.execute("SELECT status, dentist, starts_at FROM appointments WHERE patient_id = ?", (pid,)).fetchone()
            # asserted in CLINIC time (P52). the stored value is a UTC instant, so
            # matching its text would be matching an offset - and would pass or
            # fail depending on the season rather than on the product.
            import clinic_time as _ct
            check("5 confirm wrote a booked row", row is not None and row["status"] == "booked"
                  and _ct.local_date(row["starts_at"]) == day
                  and _ct.local_hhmm(row["starts_at"]) == "16:30", dict(row) if row else None)

            # --- patient overview now shows a real time
            p.goto(PATIENT + "/"); home2 = p.content()
            check("6 patient overview shows the confirmed time", "16:30" in home2)
            p.goto(PATIENT + "/appointments")
            check("6b patient tab lists it as booked with the dentist", "16:30" in p.content() and USER in p.content())

            # --- staff agenda for that day shows it; chips use the dentist
            s.goto(f"{STAFF}/appointments?day={day}")
            check("7 staff day view shows the booking", NAME in s.content() and "16:30" in s.content())
            b.close()
    finally:
        conn.execute("DELETE FROM appointments WHERE patient_id = ?", (pid,))
        conn.execute("DELETE FROM patient_sessions WHERE patient_id = ?", (pid,))
        conn.execute("DELETE FROM patient_credentials WHERE patient_id = ?", (pid,))
        conn.execute("DELETE FROM patients WHERE codice_fiscale = ?", (CF,))
        conn.execute("DELETE FROM sessions WHERE username = ?", (USER,))
        conn.execute("DELETE FROM users WHERE username = ?", (USER,))
        conn.execute("DELETE FROM dentist_schedule WHERE dentist = ?", (USER,))
        conn.commit()
        import auth
        auth.purge_audit(conn, "username IN (?, ?, ?) OR target LIKE ? OR target = ?",
                         (CF, pid, USER, f"%{CF}%", pid), "flow_appt", "fixture cleanup")
        left = conn.execute("SELECT (SELECT COUNT(*) FROM patients WHERE codice_fiscale=?) + (SELECT COUNT(*) FROM users WHERE username=?)"
                            " + (SELECT COUNT(*) FROM appointments WHERE patient_id=?)", (CF, USER, pid)).fetchone()[0]
        print("leftover rows:", left)
    print(f"{sum(RESULTS)}/{len(RESULTS)} flow checks passed")
    # EXIT NON-ZERO ON FAILURE. this printed FAIL and exited 0, so the P05 gate
    # table recorded it green while four checks were failing. a gate that
    # cannot fail its own exit code is not a gate.
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
