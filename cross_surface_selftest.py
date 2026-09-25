"""P22: one appointment, every surface. written before the fix.

the failure this pins (2026-09-24): Giulia's "next appointment 2026-10-08" came from
the free-text recall in her visit note, while the calendar - correctly - was empty.
every surface that says "next appointment" must answer from the same booked rows:
the staff patient list, the patient record, the calendar, the portal, the chat and
reminders. a cancelled, past or merely requested appointment is never shown as
booked, and a note's recall text is never shown as an appointment.
"""

import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from werkzeug.security import generate_password_hash

import appointments
import clinic_time
import demo_fixtures
import patient_accessor
import patient_id
import reminders
import storage

TODAY = datetime(2026, 9, 17, 9, 0)          # clinic time, pinned (the p52 lesson)
NOW_UTC = clinic_time.read_instant("2026-09-17T07:00:00+00:00")


def _visit(conn, pid, day, recall, src):
    conn.execute("INSERT INTO visits (patient_id, visit_date, procedures, clinical_notes, next_appointment,"
                 " source_path) VALUES (?, ?, '[]', 'controllo', ?, ?)", (pid, day, recall, src))


def _staff_client(db_path):
    import app.db as app_db
    from app import create_app
    app_db.DB_PATH = str(db_path)
    app = create_app()
    app.config["WTF_CSRF_ENABLED"] = False
    client = app.test_client()
    r = client.post("/login", data={"username": "zzx_dentist", "password": "zzx_pass_12345"})
    assert r.status_code in (302, 303), "the test dentist could not sign in"
    return client


def _row(html, name):
    for tr in re.findall(r"<tr[^>]*>.*?</tr>", html, re.S):
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", tr))
        if name in text:
            return text
    raise AssertionError(f"{name} not in the patient list")


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "x.sqlite"
        conn = storage.init_db(str(db))
        demo_fixtures.seed(conn)
        conn.execute("INSERT INTO users (username, password_hash, role, active) VALUES (?, ?, 'dentist', 1)",
                     ("zzx_dentist", generate_password_hash("zzx_pass_12345")))
        a = patient_id.seed_patient(conn, "ZZXA800101010101", "Anna Prenotata")
        b = patient_id.seed_patient(conn, "ZZXB800101010102", "Bruno Annullato")
        c = patient_id.seed_patient(conn, "ZZXC800101010103", "Carla Richiesta")
        d = patient_id.seed_patient(conn, "ZZXD800101010104", "Dario Passato")
        e = patient_id.seed_patient(conn, "ZZXE800101010105", "Elena Doppia")
        _visit(conn, a, "2026-09-08", "2026-10-08", "zzx/a.json")      # the Giulia shape: a recall, no booking
        _visit(conn, d, "2026-06-01", "6mo", "zzx/d.json")
        conn.commit()

        booked_a = appointments.book(conn, a, "dentist", "2026-09-23T10:30", 30)
        cancelled = appointments.book(conn, b, "dentist", "2026-09-24T11:00", 30)
        appointments.cancel(conn, cancelled)
        appointments.request(conn, c, "2026-09-25", "morning")
        conn.execute("INSERT INTO appointments (patient_id, dentist, starts_at, minutes, status, created_at, updated_at)"
                     " VALUES (?, 'dentist', '2026-09-10T08:30:00+00:00', 30, 'booked', ?, ?)",
                     (d, clinic_time.stamp(), clinic_time.stamp()))      # booked, but already past
        later_e = appointments.book(conn, e, "dentist", "2026-09-30T09:30", 30)
        first_e = appointments.book(conn, e, "dentist", "2026-09-23T15:00", 30)
        conn.commit()

        # 1. the one rule
        assert appointments.next_booked(conn, a)["id"] == booked_a
        assert appointments.next_booked(conn, e)["id"] == first_e, "1: not the earliest booking"
        for p in (b, c, d):
            assert appointments.next_booked(conn, p) is None, f"1: {p} has no booked future appointment"

        # 2. every next appointment is on the calendar, on its own clinic date and time
        for p in (a, e):
            nxt = appointments.next_booked(conn, p)
            local = clinic_time.local_date(nxt["starts_at"])
            assert nxt["id"] in [r["id"] for r in appointments.agenda(conn, local)], f"2: {p} not on {local}"
        assert later_e in [r["id"] for r in appointments.agenda(conn, "2026-09-30")]

        # 3. the staff patient list shows the booking, never a recall, never a cancelled / past / requested row
        client = _staff_client(db)
        html = client.get("/patients").get_data(as_text=True)
        assert "2026-09-23 10:30" in _row(html, "Anna Prenotata"), _row(html, "Anna Prenotata")
        assert "2026-10-08" not in _row(html, "Anna Prenotata"), "3: the note's recall shown as an appointment"
        assert "2026-09-23 15:00" in _row(html, "Elena Doppia"), _row(html, "Elena Doppia")
        for name in ("Bruno Annullato", "Carla Richiesta", "Dario Passato"):
            row = _row(html, name)
            assert not re.search(r"2026-09-\d\d \d\d:\d\d", row) and "6mo" not in row, f"3: {row}"

        # 4. the record keeps the recall, labelled as what it is
        page = client.get("/patients/ZZXA800101010101").get_data(as_text=True)
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page))
        assert "Recall written in the note: 2026-10-08" in text, "4: the recall is not labelled as a recall"
        assert "Next appointment: 2026-10-08" not in text

        # 5. the portal and the chat answer from the same booking
        booked, requested = appointments.open_for_patient(conn, a)
        assert [x["id"] for x in booked] == [booked_a]
        assert patient_accessor.get_next_appointment(a, conn) == "2026-09-23 10:30", "5: the chat disagrees"
        assert patient_accessor.get_next_appointment(d, conn) is None, "5: a recall reached the chat"
        assert patient_accessor.get_next_appointment(b, conn) is None, "5: a cancelled booking reached the chat"
        assert patient_accessor.get_next_appointment(c, conn) is None, "5: a request reached the chat"
        assert appointments.open_for_patient(conn, b)[0] == [] and len(appointments.open_for_patient(conn, c)[1]) == 1

        # 5b. P23: staff Q&A answers from the same booking - the surface P22 missed - and names a recall as a recall
        import ask
        qa_a = ask.answer_exact("ZZXA800101010101", "appointment", conn)
        assert "2026-09-23 10:30" in qa_a and "(booked)" in qa_a, f"5b: staff Q&A disagrees: {qa_a}"
        qa_d = ask.answer_exact("ZZXD800101010104", "appointment", conn)
        assert "has no booked appointment" in qa_d and "recall says: 6mo" in qa_d, f"5b: a recall answered as a booking: {qa_d}"
        assert "zzx/" not in qa_a and ".json" not in qa_d, "5b: a file path cited as the source"
        # 5c. the staff day view reads the same rows as the agenda
        for d in ("2026-09-23", "2026-09-24", "2026-09-30"):
            assert [r["id"] for r in appointments.day_rows(conn, d)] == [r["id"] for r in appointments.agenda(conn, d)], \
                f"5c: day view and agenda disagree on {d}"

        # 6. reminders are planned for booked future appointments only
        reminders.plan(conn, now=NOW_UTC)
        planned = {r["subject_id"] for r in conn.execute("SELECT subject_id FROM reminder_jobs WHERE kind = 'appointment'")}
        assert planned == {booked_a, first_e, later_e}, f"6: reminders planned for {planned}"
        conn.close()
    print("selftest ok")


def main():
    if "--selftest" not in sys.argv:
        print("usage: python cross_surface_selftest.py --selftest")
        return
    real = clinic_time.now
    clinic_time.now = lambda env=None: TODAY
    try:
        selftest()
    finally:
        clinic_time.now = real


if __name__ == "__main__":
    main()
