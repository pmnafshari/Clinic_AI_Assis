"""When the clinic is open, who is working, and whether a slot is bookable.

ONE PREDICATE. `refusal()` is called by book(), reschedule() AND confirm(), the
same way `_overlaps` already is, so the rule cannot drift between the three.
A patient request confirmed into a slot goes through exactly what a staff
booking goes through - the request is not a way around the schedule.

THE UI IS NOT A CONTROL LAYER (P05.04). Every rule here is enforced server-side
on the write path. A form that only offers open hours is a convenience; the
refusal is what makes it true.

THE DURATION IS PART OF THE QUESTION. A 30-minute appointment starting fifteen
minutes before closing does not fit, and checking only the start time is the
obvious version of this that is wrong.

CIVIL TIME, DELIBERATELY (Phase 52). Every rule in here is a wall-clock
question: does 09:00 fall inside opening hours, is the dentist rostered, is it
a holiday. Opening hours and roster windows are civil times of day and the
field audit keeps them that way, so `refusal()` takes the CLINIC-LOCAL start
and never a stored instant. The one place it has to read stored rows - the
capacity count - converts them back to local first.
"""

import sys
from datetime import timedelta

import clinic_time
import patient_id as _pidmod

SCHEMA = """
CREATE TABLE IF NOT EXISTS clinic_hours (
    weekday INTEGER PRIMARY KEY,      -- 0 = Monday, matching date.weekday()
    opens TEXT,                       -- "09:00", NULL when closed
    closes TEXT,
    closed INTEGER NOT NULL DEFAULT 0,
    capacity INTEGER NOT NULL DEFAULT 0   -- 0 = no limit; concurrent appointments
);

CREATE TABLE IF NOT EXISTS clinic_closures (
    closure_date TEXT PRIMARY KEY,    -- "2026-12-25"
    reason TEXT
);

CREATE TABLE IF NOT EXISTS dentist_schedule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dentist TEXT NOT NULL,
    weekday INTEGER NOT NULL,
    starts TEXT NOT NULL,
    ends TEXT NOT NULL,
    UNIQUE(dentist, weekday)
);

CREATE TABLE IF NOT EXISTS dentist_absences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dentist TEXT NOT NULL,
    from_date TEXT NOT NULL,
    to_date TEXT NOT NULL,            -- inclusive
    reason TEXT
);
"""

# A FIXTURE, NOT THE CLINIC'S HOURS. D07 is unanswered; the owner states their
# real opening hours, holidays and capacity. Seeded so the feature is testable
# and visible, and marked here so nobody mistakes it for a decision.
FIXTURE_HOURS = [
    (0, "09:00", "18:00", 0, 0),
    (1, "09:00", "18:00", 0, 0),
    (2, "09:00", "18:00", 0, 0),
    (3, "09:00", "18:00", 0, 0),
    (4, "09:00", "17:00", 0, 0),
    (5, None, None, 1, 0),
    (6, None, None, 1, 0),
]


def seed_fixture_hours(conn):
    """Opening hours for a demo clinic. Create-only, like seed_users."""
    for weekday, opens, closes, closed, capacity in FIXTURE_HOURS:
        conn.execute(
            "INSERT OR IGNORE INTO clinic_hours (weekday, opens, closes, closed, capacity)"
            " VALUES (?, ?, ?, ?, ?)", (weekday, opens, closes, closed, capacity))
    conn.commit()


def _minutes(hhmm):
    hours, mins = hhmm.split(":")
    return int(hours) * 60 + int(mins)


def hours_for(conn, weekday):
    return conn.execute(
        "SELECT opens, closes, closed, capacity FROM clinic_hours WHERE weekday = ?",
        (weekday,)).fetchone()


def is_closed_date(conn, day):
    return conn.execute(
        "SELECT reason FROM clinic_closures WHERE closure_date = ?", (day,)).fetchone()


def refusal(conn, dentist, starts_at, minutes, exclude_id=None):
    """Why this slot cannot be booked, or None. The whole schedule rule.

    Returns a sentence a receptionist can act on, not a code - it goes straight
    to the screen, and "refused" without a reason means phoning the patient
    back with nothing to say.
    """
    try:
        start = clinic_time.parse(starts_at)
    except ValueError as e:
        return str(e)
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return "the appointment length is not a number of minutes"
    if minutes <= 0:
        return "an appointment needs a length in minutes"

    end = start + timedelta(minutes=minutes)
    day = start.date().isoformat()

    # a local time that does not exist, or happens twice. working hours put
    # these out of reach, so this is a guard on the guard rather than a case a
    # receptionist will meet.
    if clinic_time.is_dst_nonexistent(start):
        return f"{start.strftime('%H:%M')} does not exist on {day} - the clocks go forward"

    closure = is_closed_date(conn, day)
    if closure is not None:
        why = closure["reason"] or "the clinic is closed"
        return f"the clinic is closed on {day} ({why})"

    hours = hours_for(conn, start.weekday())
    if hours is None:
        return "no opening hours are configured for the clinic - set them before booking"
    if hours["closed"] or not hours["opens"]:
        return f"the clinic does not open on {start.strftime('%A')}s"

    opens, closes = _minutes(hours["opens"]), _minutes(hours["closes"])
    start_min = start.hour * 60 + start.minute
    end_min = start_min + minutes
    if start_min < opens:
        return f"the clinic opens at {hours['opens']} on {start.strftime('%A')}s"
    # THE DURATION, not just the start. checking the start alone is the version
    # of this that looks right and books over closing time.
    if end_min > closes:
        return (f"a {minutes} minute appointment from {start.strftime('%H:%M')} runs past "
                f"closing at {hours['closes']}")

    roster = conn.execute(
        "SELECT starts, ends FROM dentist_schedule WHERE dentist = ? AND weekday = ?",
        (dentist, start.weekday())).fetchone()
    if roster is None:
        return f"{dentist} does not work on {start.strftime('%A')}s"
    if start_min < _minutes(roster["starts"]) or end_min > _minutes(roster["ends"]):
        return (f"{dentist} works {roster['starts']}-{roster['ends']} on "
                f"{start.strftime('%A')}s")

    absent = conn.execute(
        "SELECT reason FROM dentist_absences WHERE dentist = ?"
        " AND from_date <= ? AND to_date >= ?", (dentist, day, day)).fetchone()
    if absent is not None:
        why = absent["reason"] or "away"
        return f"{dentist} is {why} on {day}"

    if hours["capacity"]:
        # concurrent appointments across every dentist, not per dentist: the
        # limit is chairs and staff, which the whole clinic shares
        overlapping = 0
        # the day's booked rows, bounded as instants and then filtered on the
        # clinic's own date - the same rule agenda() follows, and for the same
        # reason: slicing a UTC string to ten characters asks a UTC question.
        lo, hi = clinic_time.day_bounds_utc(day)
        for row in conn.execute(
                "SELECT id, starts_at, minutes FROM appointments"
                " WHERE status = 'booked' AND starts_at >= ? AND starts_at < ?",
                (lo, hi)).fetchall():
            if exclude_id is not None and row["id"] == exclude_id:
                continue
            if clinic_time.local_date(row["starts_at"]) != day:
                continue
            # back to local, because `start` and `end` above are local
            other_start = clinic_time.local_of(row["starts_at"])
            other_end = other_start + timedelta(minutes=row["minutes"])
            if start < other_end and other_start < end:
                overlapping += 1
        if overlapping >= hours["capacity"]:
            return (f"the clinic is at capacity at {start.strftime('%H:%M')} on {day} "
                    f"({hours['capacity']} at once)")

    return None


def selftest():
    import tempfile
    from pathlib import Path

    import storage

    with tempfile.TemporaryDirectory() as tmp:
        conn = storage.init_db(str(Path(tmp) / "t.sqlite"))
        seed_fixture_hours(conn)
        # rossi is rostered to the clinic's own closing time on Monday ON
        # PURPOSE: the roster is checked after clinic hours, so a narrower
        # roster would mask the closing-time rule and check 3 would pass for
        # the wrong reason.
        conn.execute("INSERT INTO dentist_schedule (dentist, weekday, starts, ends)"
                     " VALUES ('dr rossi', 0, '09:00', '18:00')")
        conn.execute("INSERT INTO dentist_schedule (dentist, weekday, starts, ends)"
                     " VALUES ('dr rossi', 1, '09:00', '17:00')")
        conn.commit()
        MON, TUE, SAT = "2026-09-07", "2026-09-08", "2026-09-12"

        # 1. the ordinary case
        assert refusal(conn, "dr rossi", f"{MON}T09:00", 30) is None, \
            "1: 09:00 on a Monday with a rostered dentist is bookable"

        # 2. 03:00 BOOKED SILENTLY BEFORE THIS PHASE. that is the defect this
        # whole module exists for, and the reason is stated to the user.
        why = refusal(conn, "dr rossi", f"{MON}T03:00", 30)
        assert why and "opens at 09:00" in why, f"2: 03:00 must be refused, got {why}"

        # 3. THE DURATION IS PART OF THE QUESTION. 16:45 starts inside clinic
        # hours and ends outside them; checking only the start books over
        # closing time, which is the obvious-and-wrong version of this rule.
        late = refusal(conn, "dr rossi", f"{MON}T17:45", 30)
        assert late and "runs past closing" in late, \
            f"3: an appointment ending after closing must be refused, got {late}"
        assert refusal(conn, "dr rossi", f"{MON}T17:30", 30) is None, \
            "3: but one that ends exactly at closing fits"

        # 4. a day the clinic does not open
        shut = refusal(conn, "dr rossi", f"{SAT}T10:00", 30)
        assert shut and "does not open on Saturdays" in shut, f"4: got {shut}"

        # 5. a holiday closes a day the clinic would otherwise work
        conn.execute("INSERT INTO clinic_closures VALUES (?, ?)", (TUE, "Ferragosto"))
        conn.commit()
        holiday = refusal(conn, "dr rossi", f"{TUE}T10:00", 30)
        assert holiday and "Ferragosto" in holiday, f"5: the reason must reach the user, got {holiday}"
        conn.execute("DELETE FROM clinic_closures WHERE closure_date = ?", (TUE,))
        conn.commit()

        # 6. THE ROSTER IS SEPARATE FROM THE CLINIC'S HOURS. the clinic is open
        # on Wednesday; this dentist is not rostered, and an open clinic is not
        # permission to book a dentist who is not there.
        WED = "2026-09-09"
        unrostered = refusal(conn, "dr rossi", f"{WED}T10:00", 30)
        assert unrostered and "does not work on Wednesdays" in unrostered, \
            f"6: got {unrostered}"

        # 6b. and a dentist's own hours can be narrower than the clinic's
        conn.execute("INSERT INTO dentist_schedule (dentist, weekday, starts, ends)"
                     " VALUES ('dr bianchi', 0, '14:00', '17:00')")
        conn.commit()
        early = refusal(conn, "dr bianchi", f"{MON}T09:00", 30)
        assert early and "14:00-17:00" in early, f"6b: got {early}"
        assert refusal(conn, "dr bianchi", f"{MON}T14:00", 30) is None, \
            "6b: and inside their own window it is fine"

        # 7. leave
        conn.execute("INSERT INTO dentist_absences (dentist, from_date, to_date, reason)"
                     " VALUES ('dr rossi', ?, ?, 'on leave')", (MON, TUE))
        conn.commit()
        away = refusal(conn, "dr rossi", f"{MON}T10:00", 30)
        assert away and "on leave" in away, f"7: got {away}"
        assert refusal(conn, "dr bianchi", f"{MON}T14:00", 30) is None, \
            "7: one dentist's leave must not close the clinic for another"
        conn.execute("DELETE FROM dentist_absences")
        conn.commit()

        # 8. capacity is a whole-clinic limit - chairs and staff are shared, so
        # it counts across dentists rather than per dentist
        conn.execute("UPDATE clinic_hours SET capacity = 1 WHERE weekday = 0")
        conn.commit()
        cpid = _pidmod.seed_patient(conn, "AAAA000000000001", "Cap Patient")
        # stored as an INSTANT, the way book() writes one - a naive row here
        # would not be a weaker fixture, it would be an unmigrated one, and
        # read_instant refuses it
        ts = clinic_time.stamp()
        conn.execute(
            "INSERT INTO appointments (patient_id, dentist, starts_at, minutes, status,"
            " created_at, updated_at) VALUES (?,'dr rossi',?,30,'booked',?,?)",
            (cpid, clinic_time.to_utc_text(clinic_time.parse(f"{MON}T10:00:00")), ts, ts))
        conn.commit()
        assert refusal(conn, "dr bianchi", f"{MON}T14:00", 30) is None, \
            "8: an hour with nothing booked in it is unaffected by capacity"
        # rossi, not bianchi: bianchi is rostered 14:00-17:00, so a 10:15 slot
        # would be refused by the ROSTER before capacity was ever consulted and
        # this check would pass without testing capacity at all
        clash = refusal(conn, "dr rossi", f"{MON}T10:15", 30)
        assert clash and "at capacity" in clash, f"8: got {clash}"
        # and the row being moved does not count against itself
        appt_id = conn.execute("SELECT id FROM appointments").fetchone()["id"]
        assert refusal(conn, "dr rossi", f"{MON}T10:15", 30, exclude_id=appt_id) is None, \
            "8: rescheduling an appointment must not collide with itself on capacity"
        conn.execute("UPDATE clinic_hours SET capacity = 0 WHERE weekday = 0")
        conn.commit()

        # 9. bad input is refused before any schedule lookup
        for bad, expect in ((0, "length"), (-5, "length"), ("half an hour", "not a number")):
            got = refusal(conn, "dr rossi", f"{MON}T10:00", bad)
            assert got and expect in got, f"9: minutes={bad!r} -> {got}"
        aware = refusal(conn, "dr rossi", f"{MON}T10:00:00+02:00", 30)
        assert aware and "different contract" in aware, \
            "9: an aware value must be refused here too, not silently stripped"

        # 10. a clinic with no hours configured refuses rather than allowing
        # everything - an empty schedule is not an open-all-hours schedule
        bare = storage.init_db(str(Path(tmp) / "bare.sqlite"))
        none_set = refusal(bare, "dr rossi", f"{MON}T10:00", 30)
        assert none_set and "no opening hours" in none_set, \
            f"10: an unconfigured clinic must refuse, got {none_set}"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python availability.py --selftest")


if __name__ == "__main__":
    main()
