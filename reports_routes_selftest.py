"""P29 R-023/E-004/R-024/U-013: a dentist's own work on Reports and its export, limited to that dentist on the server;
report tables with sort and search; the billing export and invoices kept after an erasure, said as such."""
import csv
import io
import json
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

from werkzeug.security import generate_password_hash

import patient_id
import storage
import web_session

CF_A, CF_B, CF_HELD = "ZZRA800101010101", "ZZRB800101010102", "ZZRH800101010103"


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        import app.db as app_db
        import appointments
        from app import create_app
        db = str(Path(tmp) / "reports.sqlite")
        app_db.DB_PATH = db
        app_db.CHROMA_PATH = str(Path(tmp) / "chroma")
        conn = storage.init_db(db)
        for user, role in (("dr_alba", "dentist"), ("dr_bruno", "dentist"), ("desk", "assistant"), ("boss", "admin")):
            conn.execute("INSERT INTO users (username, password_hash, role, active) VALUES (?, ?, ?, 1)",
                         (user, generate_password_hash("x"), role))
        for d in ("dr_alba", "dr_bruno"):
            for wd in range(7):
                conn.execute("INSERT OR IGNORE INTO dentist_schedule (dentist, weekday, starts, ends) VALUES (?, ?, '08:00', '19:00')",
                             (d, wd))
        import availability
        availability.seed_fixture_hours(conn)
        pa = patient_id.seed_patient(conn, CF_A, "Alba Patient", "+393400000001")
        pb = patient_id.seed_patient(conn, CF_B, "Bruno Patient", "+393400000002")
        held = patient_id.seed_patient(conn, CF_HELD, "Held Shell", None)
        import clinic_time
        today = clinic_time.now().date()
        while today.weekday() == 6:
            today = today + timedelta(days=1)  # the clinic is closed on Sundays
        appointments.book(conn, pa, "dr_alba", f"{today.isoformat()}T10:00", 30)
        appointments.book(conn, pb, "dr_bruno", f"{today.isoformat()}T11:00", 30)
        for pid, who, procs in ((pa, "dr_alba", ["filling 14"]), (pb, "dr_bruno", ["ext 38"])):
            cur = conn.execute("INSERT INTO visits (patient_id, visit_date, procedures, clinical_notes, source_path)"
                               " VALUES (?, ?, ?, 'ok', ?)", (pid, today.isoformat(), json.dumps(procs), f"sorted/{pid}/n.json"))
            conn.execute("INSERT INTO note_reviews (origin, status, patient_id, codice_fiscale, visit_id, created_by,"
                         " created_at, decided_by, decided_at) VALUES ('upload', 'confirmed', ?, ?, ?, 'desk', ?, ?, ?)",
                         (pid, CF_A if pid == pa else CF_B, cur.lastrowid, clinic_time.stamp(), who, clinic_time.stamp()))
        import ledger
        hv = conn.execute("INSERT INTO visits (patient_id, visit_date, procedures, clinical_notes, source_path)"
                          " VALUES (?, ?, '[]', '', ?)", (held, today.isoformat(), f"sorted/{held}/n.json")).lastrowid
        inv = ledger.ensure_invoice(conn, held, hv)
        conn.execute("INSERT INTO data_requests (patient_id, kind, status, requested_by, requested_role, requested_at,"
                     " hold_reason, done_at) VALUES (?, 'erasure', 'done', 'desk', 'assistant', ?, ?, ?)",
                     (held, clinic_time.stamp(), "invoices kept under the retention policy (demo template: 10 years)",
                      clinic_time.stamp()))
        conn.commit()

        app = create_app()
        app.config["TESTING"] = True

        def client(user, role):
            c = app.test_client()
            c.set_cookie(web_session.COOKIE_NAME, web_session.create_session(conn, user, role))
            return c

        alba, bruno, desk, boss = (client("dr_alba", "dentist"), client("dr_bruno", "dentist"),
                                   client("desk", "assistant"), client("boss", "admin"))

        # 1. a dentist's own work on Reports: their appointments and the notes they confirmed, nobody else's
        page = alba.get("/reports").get_data(as_text=True)
        assert "data-my-work" in page and "Alba Patient" in page and "Bruno Patient" not in page, \
            "1: Reports does not show the dentist's own work, or shows another dentist's"
        assert 'data-list-tools data-for="my-appointments"' in page and 'data-list-tools data-for="my-notes"' in page, \
            "1: the dentist's rows have no sort and search"

        # 2. the export is that dentist's alone, whatever the address asks for, and it is audited
        def rows(resp):
            assert resp.status_code == 200 and resp.mimetype == "text/csv", f"2: {resp.status_code} {resp.mimetype}"
            assert "attachment" in resp.headers.get("Content-Disposition", ""), "2: the export is not a download"
            return list(csv.reader(io.StringIO(resp.get_data(as_text=True))))
        mine = rows(alba.get("/reports/mine.csv?dentist=dr_bruno"))
        flat = " ".join(" ".join(r) for r in mine)
        assert "Alba Patient" in flat and "filling 14" in flat, f"2: the export lacks the dentist's own rows: {mine}"
        assert "Bruno Patient" not in flat and "ext 38" not in flat, "2: HARD FAIL - another dentist's rows in the export"
        assert "dr_alba" in flat, "2: the export does not say whose it is"
        theirs = " ".join(" ".join(r) for r in rows(bruno.get("/reports/mine.csv")))
        assert "Bruno Patient" in theirs and "Alba Patient" not in theirs, "2: the other dentist's export is not theirs"
        n = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'report_export' AND username = 'dr_alba'").fetchone()[0]
        assert n == 1, f"2: the export is not audited once ({n})"
        for who in (desk, boss):
            resp = who.get("/reports/mine.csv")
            assert resp.status_code in (302, 403) and resp.mimetype != "text/csv", "2: a non-dentist got a dentist report"

        # 3. the clinic-wide procedures are a table with sort and search, beside the chart
        assert 'data-list-tools data-for="proc-rows"' in page and "filling" in page, "3: the procedures have no table tools"

        # 4. Billing: an export for the billing roles, audited; an invoice kept after an erasure says so
        if inv is not None:
            bill = desk.get("/billing").get_data(as_text=True)
            assert "data-retained" in bill, "4: an invoice kept after its patient's erasure is not said as such"
        resp = desk.get("/billing/export.csv")
        assert resp.status_code == 200 and resp.mimetype == "text/csv", f"4: billing export {resp.status_code}"
        head = next(csv.reader(io.StringIO(resp.get_data(as_text=True))))
        assert "Invoice" in head and "Status" in head, f"4: billing export header {head}"
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'billing_export'").fetchone()[0] == 1, \
            "4: the billing export is not audited"
        assert boss.get("/billing/export.csv").mimetype != "text/csv", "4: admin exported billing"
        conn.close()
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        print("usage: python reports_routes_selftest.py --selftest")
