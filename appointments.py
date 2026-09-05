"""Appointments: the clinic's first bookable record.

Until now the only thing resembling an appointment was `visits.next_appointment`,
a free-text string the notes model pulled out of a dentist's note - no time, no
duration, no dentist. Nothing could be scheduled against it.

Two rules live here rather than in the routes:

  * CANCELLING SETS A STATUS. Nothing in this module deletes a row. A cancelled
    appointment is a clinical fact, and the audit trail points at an id that has
    to still resolve.
  * ONE OVERLAP PREDICATE. book(), reschedule() and confirm() all call
    _overlaps(), so the double-booking rule cannot drift between them.
    reschedule() excludes the row it is moving, or every reschedule would
    collide with itself.
  * A REQUEST IS NOT A BOOKING. A patient has no way to see who is free - the
    schema holds no opening hours and no dentist roster - so asking them to
    pick a slot would mean inventing availability. A `requested` row therefore
    carries a preferred DATE and a period, with `dentist` unassigned, `minutes`
    0, and the time part of `starts_at` meaningless. Nothing may render it as a
    time. Both _overlaps() and agenda() filter on `booked`, so a request can
    neither block a slot nor appear in the day view as though it were real.

Times are ISO-8601 text, like `visits.visit_date` and `audit_log.ts`. SQLite
compares ISO strings correctly, so an overlap is a string comparison and there is
no epoch column to keep in sync.
"""

import sys
from datetime import datetime, timedelta

BOOKED = "booked"
CANCELLED = "cancelled"
# a patient asked for something. it is NOT on the calendar: no dentist, no
# duration, and only the date part of starts_at means anything. staff turn it
# into a real appointment by confirming it, which is where the dentist and the
# slot are chosen. see the module docstring.
REQUESTED = "requested"
DECLINED = "declined"

MORNING = "morning"
AFTERNOON = "afternoon"
PERIODS = (MORNING, AFTERNOON)


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


def _check_period(period):
    if period not in PERIODS:
        raise ValueError("pick a morning or an afternoon")
    return period


def _check_date(day):
    # a date, not a datetime: the patient names a day, never an hour
    try:
        parsed = datetime.strptime(day, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise ValueError("that is not a valid date")
    return parsed


def request(conn, codice_fiscale, day, period, reason=None):
    """A patient asks for a day and a half of it. Returns the new row id.

    Deliberately does NOT check overlaps: a request occupies nothing, and
    refusing one because a dentist happens to be busy would leak that dentist's
    calendar to whoever asked.
    """
    period = _check_period(period)
    parsed = _check_date(day)
    if parsed < datetime.now().date():
        raise ValueError("that date has already passed")
    ts = _now()
    cur = conn.execute(
        "INSERT INTO appointments"
        " (codice_fiscale, dentist, starts_at, minutes, status, note, period,"
        "  created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        # dentist '' and minutes 0 are the unassigned markers, not defaults
        # anyone should read as real. status is what makes that unambiguous.
        (codice_fiscale, "", f"{parsed.isoformat()}T00:00:00", 0, REQUESTED,
         reason, period, ts, ts),
    )
    conn.commit()
    return cur.lastrowid


def confirm(conn, appointment_id, dentist, starts_at, minutes):
    """Staff turn a request into a real appointment.

    This is the only place a requested row gains a dentist and a time, and it
    goes through the same overlap rule as book() - a confirm that double-books
    is refused exactly as a booking is.
    """
    minutes = _check_minutes(minutes)
    start, _ = _window(starts_at, minutes)
    if not dentist:
        raise ValueError("an appointment needs a dentist")
    row = conn.execute(
        "SELECT status FROM appointments WHERE id = ?", (appointment_id,)
    ).fetchone()
    if row is None:
        raise ValueError("no such request")
    if row["status"] != REQUESTED:
        raise ValueError("only a pending request can be confirmed")
    if _overlaps(conn, dentist, start, minutes, exclude_id=appointment_id):
        raise ValueError("that slot overlaps another appointment for this dentist")
    conn.execute(
        "UPDATE appointments SET dentist = ?, starts_at = ?, minutes = ?,"
        " status = ?, period = NULL, updated_at = ? WHERE id = ?",
        (dentist, start, minutes, BOOKED, _now(), appointment_id),
    )
    conn.commit()


def decline(conn, appointment_id, reason=None):
    # a status, like cancel - the patient asked, and that they asked is a fact
    row = conn.execute(
        "SELECT status FROM appointments WHERE id = ?", (appointment_id,)
    ).fetchone()
    if row is None:
        raise ValueError("no such request")
    if row["status"] != REQUESTED:
        raise ValueError("only a pending request can be declined")
    conn.execute(
        "UPDATE appointments SET status = ?, note = COALESCE(?, note), updated_at = ?"
        " WHERE id = ?",
        (DECLINED, reason, _now(), appointment_id),
    )
    conn.commit()


def pending_requests(conn):
    return conn.execute(
        "SELECT a.*, p.patient_name FROM appointments a"
        " JOIN patients p ON p.codice_fiscale = a.codice_fiscale"
        " WHERE a.status = ? ORDER BY a.starts_at, a.created_at",
        (REQUESTED,),
    ).fetchall()


def owned_by(conn, appointment_id, codice_fiscale):
    """Does this appointment belong to this patient?

    A function rather than an `if` in a route: there are three patient-facing
    actions and the check has to be identical in all of them. Returns the row
    so a caller never has to re-read it and accidentally skip the check.
    """
    row = conn.execute(
        "SELECT * FROM appointments WHERE id = ?", (appointment_id,)
    ).fetchone()
    if row is None or row["codice_fiscale"] != codice_fiscale:
        return None
    return row


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


def open_for_patient(conn, codice_fiscale):
    """-> (booked, requested) for the patient's own surface.

    Cancelled and declined rows are kept in the table but are not what the
    patient came to see. Booked rows are filtered to today onward - a past
    appointment is history, and offering Cancel beside one is nonsense.
    """
    today = datetime.now().date().isoformat()
    booked = conn.execute(
        "SELECT * FROM appointments WHERE codice_fiscale = ? AND status = ?"
        " AND starts_at >= ? ORDER BY starts_at",
        (codice_fiscale, BOOKED, f"{today}T00:00:00"),
    ).fetchall()
    requested = conn.execute(
        "SELECT * FROM appointments WHERE codice_fiscale = ? AND status = ?"
        " ORDER BY starts_at",
        (codice_fiscale, REQUESTED),
    ).fetchall()
    return booked, requested


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


        # --- requests (phase 42) ------------------------------------------
        conn.execute("INSERT INTO patients (codice_fiscale, patient_name)"
                     " VALUES (?, ?)", ("ZZB00B00B000B", "Other Patient"))
        conn.commit()
        OTHER = "ZZB00B00B000B"
        from datetime import date, timedelta as _td
        soon = (date.today() + _td(days=7)).isoformat()

        # 10. a request stores a DAY and a period, never a time. dentist and
        # minutes are unassigned markers, and the status is what says so.
        r = request(conn, CF, soon, MORNING, "check-up")
        row = conn.execute("SELECT * FROM appointments WHERE id = ?", (r,)).fetchone()
        assert row["status"] == REQUESTED, "10: a request is not a booking"
        assert row["period"] == MORNING, "10: the period is what the patient chose"
        assert row["dentist"] == "" and row["minutes"] == 0, \
            "10: a request has no dentist and no duration"
        assert row["starts_at"].startswith(soon), "10: the date is the only real part"

        # 11. THE FENCE. a request must not occupy a slot, or a patient could
        # map a dentist's calendar by watching which requests are refused.
        #
        # this one is a regression guard, NOT a mutation-proven assertion, and
        # it is worth being exact about why: two separate things already make
        # it true - a request has no dentist (check 10) and _overlaps counts
        # only booked rows (check 6). every single-line mutation that would
        # break check 11 trips one of those two first. defence in depth, so no
        # mutation reaches this line; do not read a green 11 as proof on its
        # own, and do not delete 6 or 10 believing 11 covers them.
        # asserted, not just called: with the status filter gone this raises,
        # and an uncaught ValueError names the line rather than the rule.
        try:
            assert book(conn, OTHER, "dr rossi", f"{soon}T09:00", 30), \
                "11: a pending request must not block a real booking"
        except ValueError as e:
            raise AssertionError(
                f"11: a pending request blocked a real booking - {e}") from None
        assert not agenda(conn, soon) or all(
            x["status"] == BOOKED for x in agenda(conn, soon)), \
            "11: a request must never appear on the agenda"
        assert all(x["id"] != r for x in agenda(conn, soon)), \
            "11: and specifically not this one"

        # 12. a past date is refused - the form is not the only guard
        try:
            request(conn, CF, (date.today() - _td(days=1)).isoformat(), MORNING)
            raise AssertionError("12: a request in the past must be refused")
        except ValueError:
            pass
        # 13. and so is a period nobody offered
        try:
            request(conn, CF, soon, "midnight")
            raise AssertionError("13: an unknown period must be refused")
        except ValueError:
            pass

        # 14. OWNERSHIP. this is the assertion that matters on the patient
        # surface: the refusal, not the happy path. a test that only exercises
        # the owner proves nothing about the fence.
        assert owned_by(conn, r, CF) is not None, "14: the owner reaches their own row"
        assert owned_by(conn, r, OTHER) is None, \
            "14: another patient must NOT reach it"
        assert owned_by(conn, 999999, CF) is None, "14: nor does a row that does not exist"

        # 15. confirming assigns a real dentist and slot, clears the period,
        # and goes through the SAME overlap rule as booking
        try:
            confirm(conn, r, "dr rossi", f"{soon}T09:15", 30)
            raise AssertionError("15: a confirm that double-books must be refused")
        except ValueError:
            pass
        confirm(conn, r, "dr rossi", f"{soon}T11:00", 30)
        done = conn.execute("SELECT * FROM appointments WHERE id = ?", (r,)).fetchone()
        assert done["status"] == BOOKED and done["dentist"] == "dr rossi", \
            "15: confirming makes it real"
        assert done["period"] is None, "15: and a real booking has no period"
        assert done["minutes"] == 30, "15: staff choose the duration"

        # 16. it cannot be confirmed twice
        try:
            confirm(conn, r, "dr rossi", f"{soon}T15:00", 30)
            raise AssertionError("16: only a pending request can be confirmed")
        except ValueError:
            pass

        # 17. declining is a status, never a delete
        r2 = request(conn, CF, soon, AFTERNOON)
        decline(conn, r2, "fully booked that week")
        gone = conn.execute("SELECT * FROM appointments WHERE id = ?", (r2,)).fetchone()
        assert gone is not None and gone["status"] == DECLINED, \
            "17: declining keeps the row"
        assert not any(x["id"] == r2 for x in pending_requests(conn)), \
            "17: and takes it out of the queue"

        # 18. the patient's own view shows booked and pending, not the noise
        r3 = request(conn, CF, soon, MORNING)
        bk, rq = open_for_patient(conn, CF)
        assert any(x["id"] == r for x in bk), "18: the confirmed one is booked"
        assert [x["id"] for x in rq] == [r3], "18: only the still-pending request"
        assert all(x["status"] != DECLINED for x in bk + rq), \
            "18: a declined request is not shown back as if it were live"
        # and one patient's rows never leak into another's
        obk, orq = open_for_patient(conn, OTHER)
        assert all(x["codice_fiscale"] == OTHER for x in obk + orq), \
            "18: open_for_patient must be scoped to the patient asked for"

    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
