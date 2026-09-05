"""Route-level proof for the staff appointments surface.

Two properties here are the reason this file exists rather than trusting
appointments.py's own selftest:

  * THE WITHHOLD IS A WITHHOLD. A role without manage_appointments must get a
    response with no appointment in the body at all - not one where the markup
    is present and hidden. Phase 31 proved this class of defect with a
    mutation, and the check below is written so that breaking the gate makes it
    fail.
  * EVERY WRITE IS AUDITED. book, cancel and reschedule each leave a row naming
    the actor. A mutation with no audit row is invisible after the fact.

The database is built by storage.init_db, never by hand. audit_log is
hand-rolled in three other test files and both the ip and the reason column
broke the fast suite until each was updated; a new table must not join them.
"""

import re
import sqlite3
import sys
import tempfile
from pathlib import Path

from werkzeug.security import generate_password_hash

import app.db as app_db
from app import create_app

PATIENT_CF = "ZZS00A00A000S"
PATIENT_NAME = "Selftest Patient"


def _seed_user(db_path, username, role):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO users (username, password_hash, role, active, must_change_password)"
        " VALUES (?, ?, ?, 1, 0)",
        (username, generate_password_hash("goodpass"), role),
    )
    conn.commit()
    conn.close()


def _seed_patient(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO patients (codice_fiscale, patient_name) VALUES (?, ?)",
        (PATIENT_CF, PATIENT_NAME),
    )
    conn.commit()
    conn.close()


def _csrf_from(html):
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def _login(app, username):
    client = app.test_client()
    csrf = _csrf_from(client.get("/login").text)
    resp = client.post(
        "/login", data={"username": username, "password": "goodpass", "csrf_token": csrf}
    )
    assert resp.status_code == 302, f"login for {username} should redirect"
    return client


def _conn(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    out = conn.execute(sql, params).fetchall()
    conn.close()
    return out


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = str(tmp_path / "clinic.sqlite")
        app_db.DB_PATH = db_path
        app_db.CHROMA_PATH = str(tmp_path / "chroma")

        app = create_app()
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False

        _seed_user(db_path, "drossi", "dentist")
        _seed_user(db_path, "aassist", "assistant")
        _seed_user(db_path, "aadmin", "admin")
        _seed_patient(db_path)

        dentist = _login(app, "drossi")
        assistant = _login(app, "aassist")
        admin = _login(app, "aadmin")
        DAY = "2026-09-07"

        # 1. the table came from init_db - not from this file
        cols = {r["name"] for r in _rows(db_path, "PRAGMA table_info(appointments)")}
        assert "starts_at" in cols, "1: appointments must be created by init_db"

        # 2. a dentist books, and it lands
        resp = dentist.post("/appointments/book", data={
            "codice_fiscale": PATIENT_CF, "dentist": "drossi",
            "date": DAY, "time": "09:00", "minutes": "30", "day": DAY,
        })
        assert resp.status_code == 302, "2: booking should redirect, never re-render a POST"
        booked = _rows(db_path, "SELECT * FROM appointments")
        assert len(booked) == 1, f"2: expected one appointment, got {len(booked)}"
        appt_id = booked[0]["id"]

        # 3. and it is audited, naming the actor and the appointment
        audit = _rows(db_path, "SELECT * FROM audit_log WHERE action = 'book_appointment'")
        assert len(audit) == 1, "3: booking must leave exactly one audit row"
        assert audit[0]["username"] == "drossi", "3: the audit row names the actor"
        assert audit[0]["target"] == str(appt_id), "3: and the appointment"
        assert audit[0]["allowed"] == 1, "3: an accepted write is allowed=1"

        # 4. an assistant may book too - reception work, not clinical authorship
        resp = assistant.post("/appointments/book", data={
            "codice_fiscale": PATIENT_CF, "dentist": "drossi",
            "date": DAY, "time": "11:00", "minutes": "30", "day": DAY,
        })
        assert len(_rows(db_path, "SELECT * FROM appointments")) == 2, \
            "4: an assistant must be able to book"

        # 5. THE WITHHOLD. admin holds manage_users alone, so it must not reach
        # the surface at all - and the response must carry no trace of the
        # appointment, not a hidden one. asserting on the body is what makes a
        # CSS hide fail this check.
        resp = admin.get("/appointments", follow_redirects=True)
        body = resp.text
        assert PATIENT_NAME not in body, "5: a withheld agenda must not name the patient"
        assert PATIENT_CF not in body, "5: nor carry the codice fiscale"
        assert "Book an appointment" not in body, "5: nor the booking form"

        # 6. a denied attempt is recorded, so a sweep can see it
        denied = _rows(
            db_path,
            "SELECT * FROM audit_log WHERE action = 'manage_appointments' AND allowed = 0",
        )
        assert denied, "6: a refused attempt must leave an audit row"
        assert denied[0]["username"] == "aadmin", "6: naming who was refused"

        # 7. admin cannot write either. a gate on the GET and none on the POST
        # is the shape this check exists to catch.
        admin.post("/appointments/book", data={
            "codice_fiscale": PATIENT_CF, "dentist": "drossi",
            "date": DAY, "time": "15:00", "minutes": "30", "day": DAY,
        })
        assert len(_rows(db_path, "SELECT * FROM appointments")) == 2, \
            "7: a role without the capability must not be able to book"

        # 8. an overlap reaches the user as a message and changes nothing
        resp = dentist.post("/appointments/book", data={
            "codice_fiscale": PATIENT_CF, "dentist": "drossi",
            "date": DAY, "time": "09:15", "minutes": "30", "day": DAY,
        }, follow_redirects=True)
        assert resp.status_code == 200, "8: an overlap must not 500"
        assert "overlaps" in resp.text, "8: and must say so on the page"
        assert len(_rows(db_path, "SELECT * FROM appointments")) == 2, \
            "8: a refused booking writes nothing"

        # 9. reschedule works and is audited
        dentist.post(f"/appointments/{appt_id}/reschedule", data={
            "date": DAY, "time": "14:00", "minutes": "45", "day": DAY,
        })
        moved = _rows(db_path, "SELECT * FROM appointments WHERE id = ?", (appt_id,))[0]
        assert moved["starts_at"].endswith("14:00:00"), "9: the appointment should have moved"
        assert moved["minutes"] == 45, "9: and taken its new length"
        assert _rows(db_path, "SELECT * FROM audit_log WHERE action = 'reschedule_appointment'"), \
            "9: rescheduling must be audited"

        # 10. cancel sets a status, the row survives, and it is audited
        dentist.post(f"/appointments/{appt_id}/cancel", data={"day": DAY})
        after = _rows(db_path, "SELECT * FROM appointments WHERE id = ?", (appt_id,))
        assert len(after) == 1, "10: cancelling must not delete the row"
        assert after[0]["status"] == "cancelled", "10: it sets the status"
        assert _rows(db_path, "SELECT * FROM audit_log WHERE action = 'cancel_appointment'"), \
            "10: cancelling must be audited"

        # 10b. the nav link is gated by the SAME capability as the route. a
        # link a role cannot follow is a withhold failure read from the other
        # side, and it is how the link and the gate drift apart.
        assert "/appointments" in dentist.get("/").text, \
            "10b: a permitted role should see the nav link"
        assert "/appointments" not in admin.get("/admin/users").text, \
            "10b: a role without the capability must not be offered the link"

        # 11. the agenda no longer shows it, but the day still renders
        page = dentist.get(f"/appointments?day={DAY}")
        assert page.status_code == 200, "11: the day view should render"
        assert "14:00" not in page.text, "11: a cancelled appointment leaves the agenda"

        # --- 12. patient requests (Phase 42, PAPT-04/05) ------------------
        import appointments as _appt
        from datetime import date as _date, timedelta as _td
        conn12 = _conn(db_path)
        soon12 = (_date.today() + _td(days=6)).isoformat()
        req_id = _appt.request(conn12, PATIENT_CF, soon12, _appt.MORNING, "controllo")

        # 12a. it appears in the staff queue, as a DAY and a period - never a
        # time. printing the 00:00 in starts_at would invent a slot.
        q = dentist.get("/appointments")
        assert "Patient requests" in q.text, "12a: the requests queue should render"
        assert soon12 in q.text, "12a: and show the day asked for"
        assert "morning" in q.text, "12a: and the period"
        assert "00:00" not in q.text, \
            "12a: a request must never be rendered with a time"

        # 12b. THE WITHHOLD, for the new surface too. admin holds manage_users
        # alone: the queue must be absent from the body, not hidden in it.
        #
        # one gate in index() protects both the agenda and this queue, so a
        # mutation removing it is caught by check 5 first and never reaches
        # here. 12b is the companion that pins the NEW markup to that same
        # gate - it is not independent proof of the gate itself, and deleting
        # 5 believing 12b covers it would leave the withhold untested.
        admin_body = admin.get("/appointments", follow_redirects=True).text
        assert "Patient requests" not in admin_body, \
            "12b: a withheld role must not receive the requests queue"
        assert soon12 not in admin_body, "12b: nor the requested day"

        # 12c. admin cannot confirm either - a gate on the GET and none on the
        # POST is the defect this asserts against
        admin.post(f"/appointments/{req_id}/confirm", data={
            "dentist": "drossi", "date": soon12, "time": "10:00",
            "minutes": "30", "day": soon12})
        assert _rows(db_path, "SELECT * FROM appointments WHERE id = ?",
                     (req_id,))[0]["status"] == "requested", \
            "12c: a withheld role must not be able to confirm a request"

        # 12d. a confirm that double-books is refused by the SAME rule as a
        # booking - the request must not be a way around the overlap check
        _appt.book(_conn(db_path), PATIENT_CF, "drossi",
                   f"{soon12}T10:00", 30)
        clash = dentist.post(f"/appointments/{req_id}/confirm", data={
            "dentist": "drossi", "date": soon12, "time": "10:15",
            "minutes": "30", "day": soon12}, follow_redirects=True)
        assert "overlaps" in clash.text, "12d: an overlapping confirm must say so"
        assert _rows(db_path, "SELECT * FROM appointments WHERE id = ?",
                     (req_id,))[0]["status"] == "requested", \
            "12d: and must leave it pending"

        # 12e. a clean confirm makes it real, and audits
        dentist.post(f"/appointments/{req_id}/confirm", data={
            "dentist": "drossi", "date": soon12, "time": "16:00",
            "minutes": "30", "day": soon12})
        done12 = _rows(db_path, "SELECT * FROM appointments WHERE id = ?", (req_id,))[0]
        assert done12["status"] == "booked", "12e: confirming makes it a booking"
        assert done12["dentist"] == "drossi", "12e: with a real dentist"
        assert done12["period"] is None, "12e: and no period once it is real"
        assert _rows(db_path,
                     "SELECT * FROM audit_log WHERE action = 'confirm_appointment_request'"), \
            "12e: confirming must be audited"

        # 12f. declining is a status, never a delete
        req2 = _appt.request(_conn(db_path), PATIENT_CF, soon12,
                             _appt.AFTERNOON)
        dentist.post(f"/appointments/{req2}/decline",
                     data={"reason": "fully booked", "day": soon12})
        gone12 = _rows(db_path, "SELECT * FROM appointments WHERE id = ?", (req2,))
        assert len(gone12) == 1, "12f: declining must not delete the row"
        assert gone12[0]["status"] == "declined", "12f: it sets the status"
        assert _rows(db_path,
                     "SELECT * FROM audit_log WHERE action = 'decline_appointment_request'"), \
            "12f: declining must be audited"

    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
