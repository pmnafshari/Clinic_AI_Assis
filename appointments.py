"""Appointments: the clinic's first bookable record.

Until now the only thing resembling an appointment was `visits.next_appointment`,
a free-text string the notes model pulled out of a dentist's note - no time, no
duration, no dentist. Nothing could be scheduled against it.

Two rules live here rather than in the routes:

  * CANCELLING SETS A STATUS. Nothing in this module deletes a row. A cancelled
    appointment is a clinical fact, and the audit trail points at an id that has
    to still resolve.
  * ONE OVERLAP PREDICATE. book() and reschedule() both call _overlaps(), so the
    double-booking rule cannot drift between them. reschedule() excludes the row
    it is moving, or every reschedule would collide with itself.

Times are ISO-8601 text, like `visits.visit_date` and `audit_log.ts`. SQLite
compares ISO strings correctly, so an overlap is a string comparison and there is
no epoch column to keep in sync.
"""

import sys
from datetime import datetime, timedelta

BOOKED = "booked"
CANCELLED = "cancelled"


def _now():
    return datetime.now().isoformat()


def _parse(starts_at):
    # accepts what the form posts ("2026-09-07T09:00") and what we store
    try:
        return datetime.fromisoformat(starts_at)
    except (TypeError, ValueError):
        raise ValueError("start time is not a valid date and time")


def _check_minutes(minutes):
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        raise ValueError("length must be a whole number of minutes")
    if minutes <= 0:
        raise ValueError("length must be more than zero minutes")
    return minutes


def _window(starts_at, minutes):
    start = _parse(starts_at)
    return start.isoformat(), (start + timedelta(minutes=minutes)).isoformat()


def _overlaps(conn, dentist, starts_at, minutes, exclude_id=None):
    # half-open intervals: 10:00-10:30 and 10:30-11:00 touch, they do not
    # overlap, and a clinic books back-to-back all day.
    start, end = _window(starts_at, minutes)
    sql = (
        "SELECT id, starts_at, minutes FROM appointments"
        " WHERE dentist = ? AND status = ?"
    )
    params = [dentist, BOOKED]
    if exclude_id is not None:
        sql += " AND id != ?"
        params.append(exclude_id)
    for row in conn.execute(sql, params).fetchall():
        other_start, other_end = _window(row["starts_at"], row["minutes"])
        if start < other_end and other_start < end:
            return True
    return False


def book(conn, codice_fiscale, dentist, starts_at, minutes, note=None):
    minutes = _check_minutes(minutes)
    start, _ = _window(starts_at, minutes)
    if not dentist:
        raise ValueError("an appointment needs a dentist")
    if _overlaps(conn, dentist, start, minutes):
        raise ValueError("that slot overlaps another appointment for this dentist")
    ts = _now()
    cur = conn.execute(
        "INSERT INTO appointments"
        " (codice_fiscale, dentist, starts_at, minutes, status, note, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (codice_fiscale, dentist, start, minutes, BOOKED, note, ts, ts),
    )
    conn.commit()
    return cur.lastrowid


def cancel(conn, appointment_id):
    # a status, never a DELETE - see the module docstring
    cur = conn.execute(
        "UPDATE appointments SET status = ?, updated_at = ? WHERE id = ?",
        (CANCELLED, _now(), appointment_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        raise ValueError("no such appointment")


def reschedule(conn, appointment_id, starts_at, minutes):
    minutes = _check_minutes(minutes)
    start, _ = _window(starts_at, minutes)
    row = conn.execute(
        "SELECT dentist, status FROM appointments WHERE id = ?", (appointment_id,)
    ).fetchone()
    if row is None:
        raise ValueError("no such appointment")
    if row["status"] != BOOKED:
        raise ValueError("a cancelled appointment cannot be moved")
    if _overlaps(conn, row["dentist"], start, minutes, exclude_id=appointment_id):
        raise ValueError("that slot overlaps another appointment for this dentist")
    conn.execute(
        "UPDATE appointments SET starts_at = ?, minutes = ?, updated_at = ? WHERE id = ?",
        (start, minutes, _now(), appointment_id),
    )
    conn.commit()


def agenda(conn, day):
    # one day, booked only, with the patient's name joined in so the caller
    # does not need a second query per row
    return conn.execute(
        "SELECT a.*, p.patient_name FROM appointments a"
        " JOIN patients p ON p.codice_fiscale = a.codice_fiscale"
        " WHERE a.status = ? AND a.starts_at >= ? AND a.starts_at < ?"
        " ORDER BY a.starts_at",
        (BOOKED, f"{day}T00:00:00", f"{day}T23:59:59.999999"),
    ).fetchall()


def for_patient(conn, codice_fiscale):
    return conn.execute(
        "SELECT * FROM appointments WHERE codice_fiscale = ? ORDER BY starts_at DESC",
        (codice_fiscale,),
    ).fetchall()


def selftest():
    import tempfile
    from pathlib import Path

    import storage

    with tempfile.TemporaryDirectory() as tmp:
        conn = storage.init_db(str(Path(tmp) / "t.sqlite"))
        conn.execute(
            "INSERT INTO patients (codice_fiscale, patient_name) VALUES (?, ?)",
            ("ZZA00A00A000A", "Test Patient"),
        )
        conn.commit()
        CF = "ZZA00A00A000A"

        # 1. the table came from init_db, not from this test. if a fixture ever
        # builds it by hand, the next column added breaks the fast suite - which
        # is exactly how audit_log's ip and reason columns went wrong, twice.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(appointments)")}
        assert "starts_at" in cols and "status" in cols, "1: init_db must create appointments"

        # 2. a booking lands and comes back from the agenda
        a = book(conn, CF, "dr rossi", "2026-09-07T09:00", 30)
        rows = agenda(conn, "2026-09-07")
        assert len(rows) == 1 and rows[0]["id"] == a, "2: the booking should be on the agenda"
        assert rows[0]["patient_name"] == "Test Patient", "2: agenda joins the patient name"

        # 3. same dentist, overlapping slot - refused
        try:
            book(conn, CF, "dr rossi", "2026-09-07T09:15", 30)
            raise AssertionError("3: an overlap must be refused")
        except ValueError:
            pass

        # 4. same slot, different dentist - allowed. two chairs.
        b = book(conn, CF, "dr bianchi", "2026-09-07T09:00", 30)
        assert b, "4: a second dentist may use the same slot"

        # 5. touching is not overlapping - a clinic books back to back
        c = book(conn, CF, "dr rossi", "2026-09-07T09:30", 30)
        assert c, "5: 09:00-09:30 then 09:30-10:00 must both fit"

        # 6. cancel sets a status, the row survives, and the slot frees up
        cancel(conn, a)
        still = conn.execute("SELECT status FROM appointments WHERE id = ?", (a,)).fetchone()
        assert still is not None, "6: cancelling must not delete the row"
        assert still["status"] == CANCELLED, "6: cancelling sets the status"
        assert len(agenda(conn, "2026-09-07")) == 2, "6: a cancelled row leaves the agenda"
        again = book(conn, CF, "dr rossi", "2026-09-07T09:00", 30)
        assert again, "6: the freed slot books again"

        # 7. reschedule into a clash is refused by the SAME rule as booking
        try:
            reschedule(conn, again, "2026-09-07T09:45", 30)
            raise AssertionError("7: rescheduling into an overlap must be refused")
        except ValueError:
            pass

        # 8. rescheduling onto its OWN current slot must not collide with
        # itself. this is the exclude_id case - without it every reschedule
        # would be refused by the row it is moving. dr rossi also holds
        # 09:30-10:00 here, so the duration stays 30: stretching to 45 would be
        # a real overlap with that one, and asserting it passed would be
        # asserting the overlap rule is broken.
        reschedule(conn, again, "2026-09-07T09:00", 30)
        held = conn.execute(
            "SELECT starts_at, minutes FROM appointments WHERE id = ?", (again,)
        ).fetchone()
        assert held["starts_at"].endswith("09:00:00"), "8: it should still be at 09:00"
        assert held["minutes"] == 30, "8: and still 30 minutes"

        # 8b. and a move to a genuinely free slot goes through
        reschedule(conn, again, "2026-09-07T11:00", 45)
        moved = conn.execute(
            "SELECT starts_at, minutes FROM appointments WHERE id = ?", (again,)
        ).fetchone()
        assert moved["starts_at"].endswith("11:00:00"), "8b: it should have moved to 11:00"
        assert moved["minutes"] == 45, "8b: and taken its new length"

        # 9. a cancelled appointment cannot be moved
        try:
            reschedule(conn, a, "2026-09-07T15:00", 30)
            raise AssertionError("9: a cancelled appointment must not move")
        except ValueError:
            pass

        # 10. an unknown patient is refused by the foreign key, which connect()
        # turns on with PRAGMA foreign_keys = ON
        try:
            book(conn, "NOSUCHPATIENT", "dr rossi", "2026-09-08T09:00", 30)
            raise AssertionError("10: an unknown codice fiscale must be refused")
        except Exception as e:
            assert "FOREIGN KEY" in str(e).upper(), f"10: expected a foreign key refusal, got {e}"

        # 11. bad input is refused before it reaches sql
        for bad in (0, -30, "half an hour", None):
            try:
                book(conn, CF, "dr rossi", "2026-09-09T09:00", bad)
                raise AssertionError(f"11: minutes={bad!r} must be refused")
            except ValueError:
                pass
        try:
            book(conn, CF, "dr rossi", "not a date", 30)
            raise AssertionError("11: a malformed start must be refused")
        except ValueError:
            pass

        # 12. for_patient returns the cancelled row too - a patient's history is
        # not only what is still standing. four rows survive here: the cancelled
        # `a`, plus `b`, `c` and `again`. the refused bookings above never
        # inserted anything, which this count also proves.
        history = for_patient(conn, CF)
        assert len(history) == 4, f"12: expected 4 rows in history, got {len(history)}"
        assert sum(1 for r in history if r["status"] == CANCELLED) == 1, \
            "12: the cancelled appointment is still in the history"

    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
