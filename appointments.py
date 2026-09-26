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

PHASE 52: A BOOKED START IS A UTC INSTANT. `starts_at` on a booked row is
UTC-aware text; the clinic-local wall time is what the form posts and what the
screen shows, and `clinic_time` is the only thing that converts between them.
A REQUESTED row is untouched - it stays a bare local date marker, because its
time part never meant anything (PAPT-01).

NO READ PATH SLICES A STORED TIME TO GET A DAY. A local day is not a UTC day,
so `day_bounds_utc()` bounds the query and `local_date()` decides the day. The
old `substr(starts_at, 1, 10)` and `f"{day}T00:00:00"` forms were correct for
naive local text and are silently wrong for an instant.
"""

import sqlite3
import sys
from datetime import datetime, timedelta

import clinic_time

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
    # a machine instant: when the row was written. UTC-aware since phase 52.
    return clinic_time.stamp()


def _parse(starts_at):
    # the CLINIC-LOCAL wall time a form posts ("2026-09-07T09:00"). not a
    # stored instant - read_instant is what reads one of those back.
    try:
        parsed = datetime.fromisoformat(starts_at)
    except (TypeError, ValueError):
        raise ValueError("start time is not a valid date and time")
    if parsed.tzinfo is not None:
        raise ValueError("pick a time in the clinic's own timezone, without an offset")
    return parsed


# a dental appointment is not longer than a working day. the cap is not
# cosmetic: _overlaps() bounds its scan to the day either side of the slot, and
# that is only sound because an appointment cannot run across a whole day into
# a third one. raise this and widen that window together.
MAX_MINUTES = 480


def _check_minutes(minutes):
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        raise ValueError("length must be a whole number of minutes")
    if minutes <= 0:
        raise ValueError("length must be more than zero minutes")
    if minutes > MAX_MINUTES:
        raise ValueError(f"length must be at most {MAX_MINUTES} minutes")
    return minutes


def _window(starts_at, minutes):
    """Local wall time in -> (local text, stored UTC text) out.

    Both, because the two rules downstream want different ones and guessing
    which is which is exactly the mistake this phase is about. The schedule
    rule is a civil-time question - does 09:00 fall inside opening hours - and
    the overlap rule is an instant question.

    REFUSES a nonexistent or ambiguous local time by way of clinic_time.to_utc,
    so a slot on a DST boundary is a refusal a receptionist can read rather
    than an appointment silently an hour out.
    """
    start = _parse(starts_at)
    return start.isoformat(), clinic_time.to_utc_text(start)


def _overlaps(conn, dentist, stored_start, minutes, exclude_id=None):
    # half-open intervals: 10:00-10:30 and 10:30-11:00 touch, they do not
    # overlap, and a clinic books back-to-back all day.
    #
    # BOUNDED TO THE DAY (P05). this used to read every booked row the dentist
    # had ever had and compare them all in python, so the cost grew with their
    # history rather than with the day. an appointment cannot overlap one on
    # another date - MAX_MINUTES caps it at a working day - so the window is
    # the day either side of the slot.
    # INSTANTS, not text. `stored_start` is already UTC-aware, so the window is
    # arithmetic on real moments and the DST boundary stops being a special
    # case: two appointments either overlap in real time or they do not.
    start = clinic_time.read_instant(stored_start)
    end = start + timedelta(minutes=minutes)
    sql = (
        "SELECT id, starts_at, minutes FROM appointments"
        " WHERE dentist = ? AND status = ?"
        " AND starts_at >= ? AND starts_at < ?"
    )
    params = [dentist, BOOKED,
              clinic_time.to_storage(start - timedelta(days=1)),
              clinic_time.to_storage(end + timedelta(days=1))]
    if exclude_id is not None:
        sql += " AND id != ?"
        params.append(exclude_id)
    for row in conn.execute(sql, params).fetchall():
        other_start = clinic_time.read_instant(row["starts_at"])
        other_end = other_start + timedelta(minutes=row["minutes"])
        if start < other_end and other_start < end:
            return True
    return False


def _check_schedule(conn, dentist, start, minutes, exclude_id=None):
    """The clinic's opening hours, the roster, leave and capacity (P05).

    Called by book(), reschedule() AND confirm(), the same way _overlaps() is,
    so a patient request confirmed into a slot cannot reach a time a staff
    booking would have been refused.
    """
    import availability
    why = availability.refusal(conn, dentist, start, minutes, exclude_id=exclude_id)
    if why:
        raise ValueError(why)


# SQLite raises this when two callers win the same slot at once. Before P05 the
# overlap check was a check-then-act with nothing behind it, and two threads
# booking the identical slot BOTH SUCCEEDED - reproduced, not theorised. The
# partial unique index in storage.init_db makes the second one fail here, and
# this turns that failure into the same message the single-threaded path gives.
SLOT_TAKEN = "that slot overlaps another appointment for this dentist"


def book(conn, patient, dentist, starts_at, minutes, note=None):
    # `patient` is a codice fiscale, a patient_id, or a folded CF - resolved
    # once at the boundary (Phase 51) so nothing below stores a second copy of
    # identity.
    import patient_id as _pid

    pid = _pid.resolve(conn, patient)
    if pid is None:
        raise ValueError(f"no patient with identifier {patient!r}")
    minutes = _check_minutes(minutes)
    # local decides the schedule rule, stored decides the overlap rule and is
    # what lands in the column
    local, stored = _window(starts_at, minutes)
    if not dentist:
        raise ValueError("an appointment needs a dentist")
    _check_schedule(conn, dentist, local, minutes)
    if _overlaps(conn, dentist, stored, minutes):
        raise ValueError(SLOT_TAKEN)
    ts = _now()
    try:
        cur = conn.execute(
            "INSERT INTO appointments"
            " (patient_id, dentist, starts_at, minutes, status, note, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (pid, dentist, stored, minutes, BOOKED, note, ts, ts),
        )
    except sqlite3.IntegrityError as e:
        # the unique slot index, i.e. someone else took it between the check
        # above and this insert. anything else is a real error and must not be
        # dressed up as a booking conflict.
        if "idx_appointments_slot" not in str(e) and "unique" not in str(e).lower():
            raise
        conn.rollback()
        raise ValueError(SLOT_TAKEN)
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
    local, stored = _window(starts_at, minutes)
    row = conn.execute(
        "SELECT dentist, status FROM appointments WHERE id = ?", (appointment_id,)
    ).fetchone()
    if row is None:
        raise ValueError("no such appointment")
    if row["status"] != BOOKED:
        raise ValueError("a cancelled appointment cannot be moved")
    _check_schedule(conn, row["dentist"], local, minutes, exclude_id=appointment_id)
    if _overlaps(conn, row["dentist"], stored, minutes, exclude_id=appointment_id):
        raise ValueError(SLOT_TAKEN)
    try:
        conn.execute(
            "UPDATE appointments SET starts_at = ?, minutes = ?, updated_at = ? WHERE id = ?",
            (stored, minutes, _now(), appointment_id),
        )
    except sqlite3.IntegrityError as e:
        if "idx_appointments_slot" not in str(e) and "unique" not in str(e).lower():
            raise
        conn.rollback()
        raise ValueError(SLOT_TAKEN)
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


def request(conn, patient, day, period, reason=None):
    """A patient asks for a day and a half of it. Returns the new row id.

    Deliberately does NOT check overlaps: a request occupies nothing, and
    refusing one because a dentist happens to be busy would leak that dentist's
    calendar to whoever asked.
    """
    import patient_id as _pid

    _rpid = _pid.resolve(conn, patient)
    if _rpid is None:
        raise ValueError(f"no patient with identifier {patient!r}")
    period = _check_period(period)
    parsed = _check_date(day)
    # the CLINIC's today. on a machine in another region datetime.now() can be
    # a different date, and "that date has already passed" would be wrong for
    # the people using it.
    if parsed < clinic_time.now().date():
        raise ValueError("that date has already passed")
    ts = _now()
    cur = conn.execute(
        "INSERT INTO appointments"
        " (patient_id, dentist, starts_at, minutes, status, note, period,"
        "  created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        # dentist '' and minutes 0 are the unassigned markers, not defaults
        # anyone should read as real. status is what makes that unambiguous.
        (_rpid, "", f"{parsed.isoformat()}T00:00:00", 0, REQUESTED,
         reason, period, ts, ts),
    )
    conn.commit()
    return cur.lastrowid


def confirm(conn, appointment_id, dentist, starts_at, minutes):
    """Staff turn a request into a real appointment.

    This is the only place a requested row gains a dentist and a time, and it
    goes through the same overlap rule AND the same schedule rule as book() - a
    confirm that double-books, or that lands outside the clinic's hours or the
    dentist's roster, is refused exactly as a booking is.
    """
    minutes = _check_minutes(minutes)
    local, stored = _window(starts_at, minutes)
    if not dentist:
        raise ValueError("an appointment needs a dentist")
    row = conn.execute(
        "SELECT status FROM appointments WHERE id = ?", (appointment_id,)
    ).fetchone()
    if row is None:
        raise ValueError("no such request")
    if row["status"] != REQUESTED:
        raise ValueError("only a pending request can be confirmed")
    # the SAME schedule rule as book(). a request is not a way around the
    # clinic's opening hours or a dentist's roster - the patient asked for a
    # date and a period, and this is where a real time gets chosen.
    _check_schedule(conn, dentist, local, minutes, exclude_id=appointment_id)
    if _overlaps(conn, dentist, stored, minutes, exclude_id=appointment_id):
        raise ValueError(SLOT_TAKEN)
    try:
        conn.execute(
            "UPDATE appointments SET dentist = ?, starts_at = ?, minutes = ?,"
            " status = ?, period = NULL, updated_at = ? WHERE id = ?",
            (dentist, stored, minutes, BOOKED, _now(), appointment_id),
        )
    except sqlite3.IntegrityError as e:
        if "idx_appointments_slot" not in str(e) and "unique" not in str(e).lower():
            raise
        conn.rollback()
        raise ValueError(SLOT_TAKEN)
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
        "SELECT a.*, p.patient_name, p.codice_fiscale AS cf FROM appointments a"
        " JOIN patients p ON p.patient_id = a.patient_id"
        " WHERE a.status = ? ORDER BY a.starts_at, a.created_at",
        (REQUESTED,),
    ).fetchall()


def owned_by(conn, appointment_id, patient):
    """Does this appointment belong to this patient?

    A function rather than an `if` in a route: there are three patient-facing
    actions and the check has to be identical in all of them. Returns the row
    so a caller never has to re-read it and accidentally skip the check.
    """
    row = conn.execute(
        "SELECT * FROM appointments WHERE id = ?", (appointment_id,)
    ).fetchone()
    import patient_id as _pid

    pid = _pid.resolve(conn, patient)
    if row is None or pid is None or row["patient_id"] != pid:
        return None
    return row


def agenda(conn, day):
    # one CLINIC-LOCAL day, booked only, with the patient's name joined in so
    # the caller does not need a second query per row.
    #
    # BOUNDED IN UTC, THEN FILTERED EXACTLY. day_bounds_utc is never narrower
    # than the local day but can be an hour wider at a DST edge, so the rows it
    # returns are then filtered on local_date. Slicing starts_at to ten
    # characters instead would ask a UTC question: a 00:30 local appointment is
    # stored under the previous date and would vanish from its own day.
    lo, hi = clinic_time.day_bounds_utc(day)
    rows = conn.execute(
        "SELECT a.*, p.patient_name, p.codice_fiscale AS cf FROM appointments a"
        " JOIN patients p ON p.patient_id = a.patient_id"
        " WHERE a.status = ? AND a.starts_at >= ? AND a.starts_at < ?"
        " ORDER BY a.starts_at",
        (BOOKED, lo, hi),
    ).fetchall()
    return [r for r in rows if clinic_time.local_date(r["starts_at"]) == day]


def month_counts(conn, first_day, next_first):
    """-> {"2026-09-10": {"booked": 3, "requested": 1}} for one month.

    ONE query for the whole grid. A per-day call would be 28-31 queries to draw
    a calendar, and the page already runs agenda() and pending_requests().

    Booked and requested are counted apart because they are not the same claim
    on a day: a request carries a preferred DATE and no time (see the module
    docstring), so the calendar may mark it but must never total it in with
    real appointments. Cancelled and declined rows are excluded - a cancelled
    appointment is history, not something on the month.
    """
    # TWO QUERIES, BECAUSE THE TWO STATUSES ARE STORED DIFFERENTLY. a booked
    # start is a UTC instant and its day is a conversion; a requested one is
    # already a bare local date marker and converting it would invent an hour.
    # The old single GROUP BY substr() query was correct only while both were
    # naive local text, and would have silently mis-filed booked rows near
    # midnight once they became instants.
    counts = {}

    def bump(day, status, n):
        if first_day <= day < next_first:
            counts.setdefault(day, {BOOKED: 0, REQUESTED: 0})[status] = n

    lo, _ = clinic_time.day_bounds_utc(first_day)
    _, hi = clinic_time.day_bounds_utc(next_first)
    per_day = {}
    for row in conn.execute(
            "SELECT starts_at FROM appointments WHERE status = ?"
            " AND starts_at >= ? AND starts_at < ?", (BOOKED, lo, hi)).fetchall():
        day = clinic_time.local_date(row["starts_at"])
        per_day[day] = per_day.get(day, 0) + 1
    for day, n in per_day.items():
        bump(day, BOOKED, n)

    for row in conn.execute(
            "SELECT substr(starts_at, 1, 10) AS d, COUNT(*) AS n FROM appointments"
            " WHERE status = ? AND starts_at >= ? AND starts_at < ? GROUP BY d",
            (REQUESTED, f"{first_day}T00:00:00", f"{next_first}T00:00:00")).fetchall():
        bump(row["d"], REQUESTED, row["n"])
    return counts


def for_patient(conn, patient):
    import patient_id as _pid

    pid = _pid.resolve(conn, patient)
    return conn.execute(
        "SELECT * FROM appointments WHERE patient_id = ? ORDER BY starts_at DESC",
        (pid,),
    ).fetchall()


def range_rows(conn, first_day, next_first, statuses=(BOOKED,), dentist=None):
    """P23: THE query behind Day, Week, Month, the Home agenda and the 14-day chart - appointments in the given
    statuses whose clinic-local day is in [first_day, next_first), optionally one clinician's, in start order.
    requests are never included: they carry a preferred day, not a slot. bounded in UTC, then filtered exactly on
    the local date (the bounds can be an hour wide at a DST edge), the same way agenda() does."""
    from datetime import date as _date, timedelta as _td
    last = (_date.fromisoformat(next_first) - _td(days=1)).isoformat()
    lo, _ = clinic_time.day_bounds_utc(first_day)
    _, hi = clinic_time.day_bounds_utc(last)
    marks = ",".join("?" * len(statuses))
    sql = ("SELECT a.*, p.patient_name, p.codice_fiscale AS cf FROM appointments a"
           " JOIN patients p ON p.patient_id = a.patient_id"
           f" WHERE a.status IN ({marks}) AND a.status != ? AND a.starts_at >= ? AND a.starts_at < ?")
    args = [*statuses, REQUESTED, lo, hi]
    if dentist:
        sql += " AND a.dentist = ?"
        args.append(dentist)
    rows = conn.execute(sql + " ORDER BY a.starts_at, a.dentist", args).fetchall()
    return [r for r in rows if first_day <= clinic_time.local_date(r["starts_at"]) < next_first]


def day_rows(conn, day, statuses=(BOOKED,), dentist=None):
    """P23: one clinic day of range_rows. for (BOOKED,) and no dentist this is exactly agenda()."""
    from datetime import date as _date, timedelta as _td
    return range_rows(conn, day, (_date.fromisoformat(day) + _td(days=1)).isoformat(), statuses, dentist)


def next_confirmed(conn, after_day, dentist=None):
    """P23: the first BOOKED appointment on a clinic day after `after_day` (optionally one clinician's) - where an
    empty Day, Week or chart sends the user. -> row or None. never a request or a cancelled row."""
    from datetime import date as _date, timedelta as _td
    lo, _ = clinic_time.day_bounds_utc((_date.fromisoformat(after_day) + _td(days=1)).isoformat())
    sql = ("SELECT a.*, p.patient_name FROM appointments a JOIN patients p ON p.patient_id = a.patient_id"
           " WHERE a.status = ? AND a.starts_at >= ?")
    args = [BOOKED, lo]
    if dentist:
        sql += " AND a.dentist = ?"
        args.append(dentist)
    for r in conn.execute(sql + " ORDER BY a.starts_at LIMIT 5", args):
        if clinic_time.local_date(r["starts_at"]) > after_day:
            return r
    return None


def next_booked(conn, patient):
    """THE next appointment, for every surface that shows one (P22): the first
    booked row from the start of the clinic's today - the rule open_for_patient
    applies. never a request, a cancelled row, a past day, or the free-text recall
    in a visit note."""
    booked, _requested = open_for_patient(conn, patient)
    return booked[0] if booked else None


def next_booked_local(conn, patient):
    """-> "YYYY-MM-DD HH:MM" in clinic time, or None."""
    row = next_booked(conn, patient)
    if row is None:
        return None
    return f"{clinic_time.local_date(row['starts_at'])} {clinic_time.local_hhmm(row['starts_at'])}"


def open_for_patient(conn, patient):
    """-> (booked, requested) for the patient's own surface.

    Cancelled and declined rows are kept in the table but are not what the
    patient came to see. Booked rows are filtered to today onward - a past
    appointment is history, and offering Cancel beside one is nonsense.
    """
    import patient_id as _pid

    pid = _pid.resolve(conn, patient)
    # the CLINIC's today, and its start as an instant - "from the beginning of
    # today" is a local question with a UTC answer
    today = clinic_time.now().date().isoformat()
    lo, _ = clinic_time.day_bounds_utc(today)
    booked = conn.execute(
        "SELECT * FROM appointments WHERE patient_id = ? AND status = ?"
        " AND starts_at >= ? ORDER BY starts_at",
        (pid, BOOKED, lo),
    ).fetchall()
    requested = conn.execute(
        "SELECT * FROM appointments WHERE patient_id = ? AND status = ?"
        " ORDER BY starts_at",
        (pid, REQUESTED),
    ).fetchall()
    return booked, requested


import patient_id as _pidmod


def selftest():
    import tempfile
    from pathlib import Path

    import storage

    import availability

    with tempfile.TemporaryDirectory() as tmp:
        conn = storage.init_db(str(Path(tmp) / "t.sqlite"))
        _pidmod.seed_patient(conn, "ZZA00A00A000A", "Test Patient")
        # P05: the clinic has to be open and the dentists rostered, or every
        # booking below is refused before it reaches the rule under test. an
        # UNCONFIGURED clinic refusing everything is the correct default and is
        # asserted in availability's own check 10.
        availability.seed_fixture_hours(conn)
        for who in ("dr rossi", "dr bianchi", "drossi"):
            for weekday in range(7):
                conn.execute(
                    "INSERT OR IGNORE INTO dentist_schedule (dentist, weekday, starts, ends)"
                    " VALUES (?, ?, '00:00', '23:59')", (who, weekday))
        # and these fixtures book at all hours on purpose - they are testing the
        # overlap rule, not the schedule - so the clinic is open around the
        # clock HERE ONLY
        conn.execute("UPDATE clinic_hours SET opens='00:00', closes='23:59', closed=0")
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
        # asserted in CLINIC TIME. the stored value is a UTC instant, so
        # asserting on its text would assert an offset rather than the time the
        # clinic booked.
        assert clinic_time.local_hhmm(held["starts_at"]) == "09:00", \
            f"8: it should still be at 09:00, stored {held['starts_at']}"
        assert held["minutes"] == 30, "8: and still 30 minutes"

        # 8b. and a move to a genuinely free slot goes through
        reschedule(conn, again, "2026-09-07T11:00", 45)
        moved = conn.execute(
            "SELECT starts_at, minutes FROM appointments WHERE id = ?", (again,)
        ).fetchone()
        assert clinic_time.local_hhmm(moved["starts_at"]) == "11:00", \
            f"8b: it should have moved to 11:00, stored {moved['starts_at']}"
        assert moved["minutes"] == 45, "8b: and taken its new length"

        # 9. a cancelled appointment cannot be moved
        try:
            reschedule(conn, a, "2026-09-07T15:00", 30)
            raise AssertionError("9: a cancelled appointment must not move")
        except ValueError:
            pass

        # 10. an unknown patient is refused, and NOTHING is written. since
        # Phase 51 the refusal comes from the identity resolver with a legible
        # message, before any SQL runs; the foreign key is still there behind
        # it. what this pins is the property - a booking for somebody who is
        # not on record does not happen - not which layer says no.
        before10 = conn.execute("SELECT COUNT(*) c FROM appointments").fetchone()["c"]
        try:
            book(conn, "NOSUCHPATIENT", "dr rossi", "2026-09-08T09:00", 30)
            raise AssertionError("10: an unknown codice fiscale must be refused")
        except ValueError as e:
            assert "NOSUCHPATIENT" in str(e), f"10: the refusal must name the input, got {e}"
        assert conn.execute("SELECT COUNT(*) c FROM appointments").fetchone()["c"] == before10, \
            "10: and a refused booking must write nothing"

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
        _pidmod.seed_patient(conn, "ZZB00B00B000B", "Other Patient")
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
        other_pid = _pidmod.resolve(conn, OTHER)
        assert all(x["patient_id"] == other_pid for x in obk + orq), \
            "18: open_for_patient must be scoped to the patient asked for"

        # --- month_counts (phase 48) --------------------------------------
        #
        # a separate database, because the checks above leave a deliberate mess
        # and these assertions are about exact totals. rows go in with plain
        # INSERTs rather than book()/request(): most of the months below are in
        # the past, which request() refuses by design (check 12), and
        # month_counts is a read - what it has to get right is what the table
        # actually holds.
        mc = storage.init_db(str(Path(tmp) / "months.sqlite"))
        _pidmod.seed_patient(mc, CF, "Test Patient")

        def put(starts_at, status):
            # seeded the way the write paths store it: a row that carries a real
            # slot holds a UTC INSTANT, a request or a decline holds the bare
            # local date marker it was always given. Seeding both as naive text
            # would be seeding an unmigrated database and would prove nothing
            # about how month_counts reads a converted one.
            if status in (BOOKED, CANCELLED):
                starts_at = clinic_time.to_utc_text(clinic_time.parse(starts_at))
            ts = clinic_time.stamp()
            mc.execute(
                "INSERT INTO appointments (patient_id, dentist, starts_at,"
                " minutes, status, created_at, updated_at)"
                " VALUES (?, 'dr rossi', ?, 30, ?, ?, ?)",
                (_pidmod.resolve(mc, CF), starts_at, status, ts, ts),
            )

        # 19. a month of 30 days. two bookings share a day, one sits alone, and
        # a request on the same day as a booking is counted apart from it.
        for at in ("2026-09-10T09:00:00", "2026-09-10T11:00:00", "2026-09-24T09:00:00"):
            put(at, BOOKED)
        put("2026-09-10T00:00:00", REQUESTED)
        # neither of these may be counted anywhere
        put("2026-09-10T14:00:00", CANCELLED)
        put("2026-09-11T00:00:00", DECLINED)
        mc.commit()
        sept = month_counts(mc, "2026-09-01", "2026-10-01")
        assert sept["2026-09-10"] == {BOOKED: 2, REQUESTED: 1}, \
            f"19: bookings and requests are counted apart, got {sept.get('2026-09-10')}"
        assert sept["2026-09-24"] == {BOOKED: 1, REQUESTED: 0}, "19: and a lone booking counts 1"
        assert "2026-09-11" not in sept, "19: a declined request is not on the month"
        assert sum(d[BOOKED] for d in sept.values()) == 3, \
            "19: a cancelled appointment is not on the month either"

        # 20. month lengths. february is the one a naive +30 gets wrong, and
        # 2028 is the leap year that catches an off-by-one on the 29th.
        put("2026-02-28T09:00:00", BOOKED)
        put("2028-02-29T09:00:00", BOOKED)
        put("2026-12-31T09:00:00", BOOKED)
        mc.commit()
        assert month_counts(mc, "2026-02-01", "2026-03-01") == \
            {"2026-02-28": {BOOKED: 1, REQUESTED: 0}}, "20: the 28th of a 28-day february"
        assert month_counts(mc, "2028-02-01", "2028-03-01") == \
            {"2028-02-29": {BOOKED: 1, REQUESTED: 0}}, "20: and the 29th of a leap one"
        assert month_counts(mc, "2026-12-01", "2027-01-01") == \
            {"2026-12-31": {BOOKED: 1, REQUESTED: 0}}, "20: the 31st of december"

        # 21. THE DECEMBER BOUNDARY. the next month is a different year, so a
        # window built by bumping the month alone reads 2026-12-01..2026-01-01,
        # which is empty - and an empty december looks like a quiet month
        # rather than a bug. january must not pick december's row up either.
        assert month_counts(mc, "2027-01-01", "2027-02-01") == {}, \
            "21: january must not see december"
        put("2027-01-01T09:00:00", BOOKED)
        mc.commit()
        jan = month_counts(mc, "2027-01-01", "2027-02-01")
        assert jan == {"2027-01-01": {BOOKED: 1, REQUESTED: 0}}, \
            "21: the first of january belongs to january"
        assert "2027-01-01" not in month_counts(mc, "2026-12-01", "2027-01-01"), \
            "21: and not to december"

        # 22. A FIXED NUMBER OF QUERIES for the whole grid, not one per day. A
        # calendar drawn day by day is 28-31 round trips on a page that already
        # runs two other queries, and nothing in the rendered output would show
        # the difference.
        #
        # Two, not one, since P52: booked rows are UTC instants whose local day
        # is a conversion, requested rows are already local date markers, and
        # one GROUP BY cannot answer both without being wrong about one of
        # them. The invariant that matters is that this does not grow with the
        # length of the month.
        seen = []
        mc.set_trace_callback(lambda sql: seen.append(sql))
        month_counts(mc, "2026-09-01", "2026-10-01")
        mc.set_trace_callback(None)
        assert len(seen) == 2, f"22: month_counts must run exactly 2 queries, ran {len(seen)}"
        longer = []
        mc.set_trace_callback(lambda sql: longer.append(sql))
        month_counts(mc, "2026-01-01", "2026-02-01")
        mc.set_trace_callback(None)
        assert len(longer) == len(seen), \
            "22: and the count must not grow with the number of days in the month"

        # 23. an empty month is empty, not missing. the template walks the grid
        # and looks each day up, so {} has to be a usable answer.
        assert month_counts(mc, "2026-05-01", "2026-06-01") == {}, \
            "23: a month with nothing in it returns an empty mapping"

        # --- P05: the schedule rule and the race -------------------------
        #
        # a SEPARATE database with realistic hours. the fixture above opens the
        # clinic around the clock so the overlap checks can book at any hour;
        # these checks are about the schedule itself, so they need a clinic
        # that actually closes.
        sched = storage.init_db(str(Path(tmp) / "sched.sqlite"))
        availability.seed_fixture_hours(sched)
        _pidmod.seed_patient(sched, 'ZZC00C00C000C', 'Schedule Patient')
        sched.execute("INSERT INTO dentist_schedule (dentist, weekday, starts, ends)"
                      " VALUES ('dr rossi', 0, '09:00', '18:00')")
        sched.commit()
        SCF, MON = "ZZC00C00C000C", "2026-09-07"

        # 24. 03:00 BOOKED SILENTLY BEFORE THIS PHASE - measured, not assumed.
        # this is the defect the whole schedule rule exists for.
        try:
            book(sched, SCF, "dr rossi", f"{MON}T03:00", 30)
            raise AssertionError("24: 03:00 must be refused once the clinic has opening hours")
        except ValueError as e:
            assert "opens at 09:00" in str(e), f"24: and must say why, got {e}"
        assert book(sched, SCF, "dr rossi", f"{MON}T09:00", 30), \
            "24: an hour the clinic is actually open still books"

        # 25. THE SAME RULE ON ALL THREE WRITE PATHS. a rule applied to book()
        # and not to confirm() is a rule a patient request walks straight past,
        # which is the drift _overlaps() was already written to avoid.
        soon25 = date.today() + _td(days=30)
        while soon25.weekday() != 0:          # a Monday, so the roster applies
            soon25 += _td(days=1)
        soon25 = soon25.isoformat()
        r25 = request(sched, SCF, soon25, MORNING)
        try:
            confirm(sched, r25, "dr rossi", f"{soon25}T03:00", 30)
            raise AssertionError("25: confirming into 03:00 must be refused too")
        except ValueError as e:
            assert "opens at 09:00" in str(e), f"25: confirm must use the same rule, got {e}"
        confirm(sched, r25, "dr rossi", f"{soon25}T10:00", 30)
        landed = sched.execute("SELECT starts_at FROM appointments WHERE id = ?",
                               (r25,)).fetchone()["starts_at"]
        # in clinic time: the request named a day, and staff chose 10:00 on it
        assert clinic_time.local_date(landed) == soon25 \
            and clinic_time.local_hhmm(landed) == "10:00", \
            f"25: a valid confirm lands, stored {landed}"
        try:
            reschedule(sched, r25, f"{soon25}T22:00", 30)
            raise AssertionError("25: rescheduling outside hours must be refused")
        except ValueError as e:
            assert "runs past closing" in str(e) or "opens at" in str(e), \
                f"25: reschedule must use the same rule, got {e}"

        # 26. THE RACE. two threads booking the identical slot both succeeded
        # before this phase - reproduced on 2026-09-13, two booked rows. the
        # partial unique index makes the second one impossible; this is the
        # check that would have caught the live defect.
        import threading
        race_db = str(Path(tmp) / "race.sqlite")
        rconn = storage.init_db(race_db)
        availability.seed_fixture_hours(rconn)
        rconn.execute("UPDATE clinic_hours SET opens='00:00', closes='23:59', closed=0")
        _pidmod.seed_patient(rconn, 'ZZD00D00D000D', 'Race Patient')
        for wd in range(7):
            rconn.execute("INSERT INTO dentist_schedule (dentist, weekday, starts, ends)"
                          " VALUES ('dr rossi', ?, '00:00', '23:59')", (wd,))
        rconn.commit()
        rconn.close()

        barrier = threading.Barrier(4)
        outcomes = []

        def grab():
            c = storage.connect(race_db)
            try:
                barrier.wait()
                book(c, "ZZD00D00D000D", "dr rossi", "2026-10-05T09:00", 30)
                outcomes.append("booked")
            except ValueError:
                outcomes.append("refused")
            except Exception as exc:              # anything else is a real bug
                outcomes.append(f"error:{exc}")   # and must not read as a refusal
            finally:
                c.close()

        threads = [threading.Thread(target=grab) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        check = storage.connect(race_db)
        booked_rows = check.execute(
            "SELECT COUNT(*) c FROM appointments WHERE status = 'booked'").fetchone()["c"]
        check.close()
        assert booked_rows == 1, \
            f"26: four threads, one slot, expected exactly 1 booking, got {booked_rows}"
        assert outcomes.count("booked") == 1, f"26: exactly one winner, got {outcomes}"
        assert all(o in ("booked", "refused") for o in outcomes), \
            f"26: the losers must get a refusal, not a raw database error - {outcomes}"

        # 27. the length cap. _overlaps() bounds its scan to the day either side
        # of the slot, and that is only sound while an appointment cannot run
        # across a whole day into a third one.
        try:
            book(sched, SCF, "dr rossi", f"{MON}T09:00", MAX_MINUTES + 1)
            raise AssertionError("27: an appointment longer than a working day must be refused")
        except ValueError as e:
            assert "at most" in str(e), f"27: got {e}"

        # 28. A DATABASE THAT ALREADY HOLDS A DOUBLE BOOKING MUST STILL BOOT.
        # the race existed before the index did, so a real clinic database may
        # carry its output. CREATE UNIQUE INDEX would raise, init_db would fail
        # and the app would not start at all - a worse answer than a report.
        legacy_db = str(Path(tmp) / "legacy.sqlite")
        raw = sqlite3.connect(legacy_db)
        raw.executescript("""
            CREATE TABLE patients (patient_id TEXT PRIMARY KEY NOT NULL,
                codice_fiscale TEXT UNIQUE NOT NULL, patient_name TEXT NOT NULL, phone TEXT);
            CREATE TABLE appointments (id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id TEXT NOT NULL, dentist TEXT NOT NULL, starts_at TEXT NOT NULL,
                minutes INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'booked', note TEXT,
                period TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            INSERT INTO patients VALUES ('pid_0000000000000001','ZZE00E00E000E','Legacy Patient',NULL);
            INSERT INTO appointments (patient_id, dentist, starts_at, minutes, status,
                created_at, updated_at)
                VALUES ('pid_0000000000000001','dr rossi','2026-05-05T09:00:00',30,'booked','','');
            INSERT INTO appointments (patient_id, dentist, starts_at, minutes, status,
                created_at, updated_at)
                VALUES ('pid_0000000000000001','dr rossi','2026-05-05T09:00:00',30,'booked','','');
        """)
        raw.commit()
        raw.close()
        legacy = storage.init_db(legacy_db)           # must not raise
        assert legacy.execute(
            "SELECT COUNT(*) c FROM appointments").fetchone()["c"] == 2, \
            "28: a migration must not delete one of two real appointments"
        built = legacy.execute(
            "SELECT COUNT(*) c FROM sqlite_master WHERE type='index'"
            " AND name='idx_appointments_slot'").fetchone()["c"]
        assert built == 0, "28: and the guard is honestly absent, not silently assumed"
        legacy.close()

    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
