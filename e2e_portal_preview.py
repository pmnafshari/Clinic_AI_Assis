"""Gate G10, patient-portal shots and contrast. seeds one ZZV patient, shoots, deletes in finally."""
import sys
from datetime import date, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

import patient_auth
import patient_id
import storage
from a11y_audit import CONTRAST

URL = "http://127.0.0.1:5001"
DB = "db/clinic.sqlite"
OUT = Path("/private/tmp/claude-501/-Users-hitson-Documents-Codes-Clinic-Demo/175d348b-a96c-43a2-a70f-effda593942c/scratchpad/shots")
CF = "ZZVP800101010107"
NEW_PIN = "86420135"


def main():
    widths = [int(a) for a in sys.argv[1:]] or [1440, 390]
    OUT.mkdir(parents=True, exist_ok=True)
    conn = storage.connect(DB)
    try:
        # P52: machine instants are UTC-aware, a BOOKED start is a real instant,
        # and a REQUESTED one stays the bare date marker it always was.
        import clinic_time as _ct
        now = _ct.stamp()
        pid = patient_id.seed_patient(conn, CF, "Elena Martini", "3337778888")
        soon = date.today() + timedelta(days=4)
        booked_at = _ct.to_utc_text(_ct.parse(f"{soon}T10:30:00"))
        conn.execute("INSERT INTO appointments (patient_id, dentist, starts_at, minutes, status, note, created_at, updated_at)"
                     " VALUES (?,?,?,?,?,?,?,?)", (pid, "dentist", booked_at, 30, "booked", None, now, now))
        conn.execute("INSERT INTO appointments (patient_id, dentist, starts_at, minutes, status, note, period, created_at, updated_at)"
                     " VALUES (?,?,?,?,?,?,?,?,?)", (pid, "", f"{soon + timedelta(days=9)}T00:00:00", 0, "requested", "pulizia", "afternoon", now, now))
        conn.commit()
        pin = patient_auth.issue_pin(CF, conn, "dentist", "dentist")
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            first = True
            for w in widths:
                ctx = b.new_context(viewport={"width": w, "height": 900})
                pg = ctx.new_page()
                pg.goto(f"{URL}/login"); pg.wait_for_load_state("networkidle")
                pg.screenshot(path=str(OUT / f"p{w}-login.png"))
                pg.fill("#codice_fiscale", CF)
                pg.fill("#pin", pin if first else NEW_PIN)
                pg.click("form[action*='login'] button[type=submit]"); pg.wait_for_load_state("networkidle")
                if first:
                    pg.screenshot(path=str(OUT / f"p{w}-change-pin.png"))
                    pg.fill("#pin", NEW_PIN); pg.fill("#confirm", NEW_PIN)
                    pg.click("form[action*='change-pin'] button[type=submit]"); pg.wait_for_load_state("networkidle")
                    first = False
                for name, path in (("home", "/"), ("appointments", "/appointments"), ("billing", "/billing"), ("chat", "/chat"), ("profile", "/profile")):
                    pg.goto(URL + path); pg.wait_for_load_state("networkidle")
                    sw = pg.evaluate("document.documentElement.scrollWidth")
                    bad = pg.evaluate(CONTRAST)
                    pg.screenshot(path=str(OUT / f"p{w}-{name}.png"), full_page=True)
                    print(f"{w} {name} scroll={sw} {'OVERFLOW' if sw > w + 1 else 'ok'} contrast_fail={len(bad)}")
                    # a gate that prints a failure and exits 0 is not a gate (found in P07)
                    FAILURES.append(bool(bad) or sw > w + 1)
                    for x in bad[:5]:
                        print("    ", x)
                ctx.close()
            b.close()
    finally:
        for t in ("appointments", "patient_sessions", "patient_credentials", "handoff_requests",
                  "patient_agent_actions"):
            conn.execute(f"DELETE FROM {t} WHERE patient_id = ?", (pid,))
        conn.execute("DELETE FROM patients WHERE codice_fiscale = ?", (CF,))
        conn.commit()
        import auth
        where = "username IN (?, ?) OR target IN (?, ?)"
        auth.purge_audit(conn, where, (CF, pid, CF, pid), "ppreview", "fixture cleanup")
        left = conn.execute("SELECT (SELECT COUNT(*) FROM patients WHERE codice_fiscale = ?) + "
                            f"(SELECT COUNT(*) FROM audit_log WHERE {where})", (CF, CF, pid, CF, pid)).fetchone()[0]
        print("leftover rows:", left)
        FAILURES.append(left != 0)


FAILURES = []

if __name__ == "__main__":
    main()
    sys.exit(1 if any(FAILURES) else 0)
