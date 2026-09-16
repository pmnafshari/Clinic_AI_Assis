"""Realistic DEMO scheduling fixtures for an Italian dental clinic.

THESE ARE DEMO FIXTURES, NOT A CLINIC'S REAL SCHEDULE. The product decision of
2026-09-16 (.planning/D02-D07-D08-ANSWER.md) resolved D02/D07/D08 for demo
scope: production-like, synthetic data only, local storage, no real providers.
It permits realistic scheduling fixtures on one condition - that they are
clearly labelled and stay configurable. That is what this module is.

Every value here is replaceable without a code change: they land in
`clinic_hours`, `dentist_schedule`, `clinic_closures` and `dentist_absences`,
and a real deployment overwrites them from its own configuration. Nothing reads
this module at request time.

WHY THESE PARTICULAR VALUES. They are shaped to be plausible for a small Italian
studio dentistico rather than tidy: a long lunch closure is the norm and is
represented by a shorter Friday, Saturday is morning-only for hygiene work,
Sunday is closed, and the fixed closures are the Italian public holidays a
clinic actually shuts for. Plausible edge cases matter more than neat ones -
this is the data every scheduling test is written against.

CIVIL TIME, NEVER CONVERTED. Opening hours and roster windows are wall-clock
times of day. "We open at 09:00" is 09:00 in winter and in summer. Phase 52's
field audit (P52 section 1b) classifies them as clinic-local civil time
precisely so the timezone migration leaves them alone.
"""

import sys

# weekday, opens, closes, closed, capacity.  0 = Monday, matching date.weekday().
# capacity is concurrent appointments across the whole clinic - chairs and staff
# are shared - and 0 means no limit.
DEMO_HOURS = [
    (0, "09:00", "19:00", 0, 3),   # Mon
    (1, "09:00", "19:00", 0, 3),   # Tue
    (2, "09:00", "19:00", 0, 3),   # Wed
    (3, "09:00", "19:00", 0, 3),   # Thu
    (4, "09:00", "17:00", 0, 3),   # Fri - the clinic closes earlier
    (5, "09:00", "13:00", 0, 2),   # Sat - mornings only, reduced capacity
    (6, None, None, 1, 0),         # Sun - closed
]

# dentist -> [(weekday, starts, ends)]. Deliberately NOT all identical: a roster
# where everyone works the same hours never exercises the per-dentist rule.
DEMO_ROSTER = {
    "dentist": [(0, "09:00", "19:00"), (1, "09:00", "19:00"), (2, "09:00", "19:00"),
                (3, "09:00", "19:00"), (4, "09:00", "17:00")],
    # a part-timer: afternoons midweek, plus the Saturday morning clinic
    "dr_ferrari": [(1, "14:00", "19:00"), (2, "14:00", "19:00"), (3, "14:00", "19:00"),
                   (5, "09:00", "13:00")],
}

# Italian public holidays a clinic closes for. Dates, never instants (P52 1c).
DEMO_CLOSURES = [
    ("2026-01-01", "Capodanno"),
    ("2026-01-06", "Epifania"),
    ("2026-04-06", "Lunedi dell'Angelo"),
    ("2026-04-25", "Festa della Liberazione"),
    ("2026-05-01", "Festa del Lavoro"),
    ("2026-06-02", "Festa della Repubblica"),
    ("2026-08-15", "Ferragosto"),
    ("2026-11-01", "Ognissanti"),
    ("2026-12-08", "Immacolata"),
    ("2026-12-25", "Natale"),
    ("2026-12-26", "Santo Stefano"),
]

# the August shutdown an Italian studio actually takes, as a leave range
DEMO_ABSENCES = [
    ("dentist", "2026-08-10", "2026-08-23", "chiusura estiva"),
    ("dr_ferrari", "2026-08-10", "2026-08-30", "chiusura estiva"),
]

LABEL = ("DEMO FIXTURE - not a real clinic's schedule. Replaceable without a code "
         "change; see demo_fixtures.py and .planning/D02-D07-D08-ANSWER.md.")


def seed(conn, commit=True):
    """Create-only, like seed_users. Never overwrites a configured schedule."""
    for weekday, opens, closes, closed, capacity in DEMO_HOURS:
        conn.execute(
            "INSERT OR IGNORE INTO clinic_hours (weekday, opens, closes, closed, capacity)"
            " VALUES (?, ?, ?, ?, ?)", (weekday, opens, closes, closed, capacity))
    for who, days in DEMO_ROSTER.items():
        for weekday, starts, ends in days:
            conn.execute(
                "INSERT OR IGNORE INTO dentist_schedule (dentist, weekday, starts, ends)"
                " VALUES (?, ?, ?, ?)", (who, weekday, starts, ends))
    for day, reason in DEMO_CLOSURES:
        conn.execute("INSERT OR IGNORE INTO clinic_closures (closure_date, reason)"
                     " VALUES (?, ?)", (day, reason))
    for who, start, end, reason in DEMO_ABSENCES:
        existing = conn.execute(
            "SELECT 1 FROM dentist_absences WHERE dentist = ? AND from_date = ?",
            (who, start)).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO dentist_absences (dentist, from_date, to_date, reason)"
                " VALUES (?, ?, ?, ?)", (who, start, end, reason))
    if commit:
        conn.commit()
    return {"hours": len(DEMO_HOURS), "roster": sum(len(v) for v in DEMO_ROSTER.values()),
            "closures": len(DEMO_CLOSURES), "absences": len(DEMO_ABSENCES)}


def selftest():
    import tempfile
    from pathlib import Path

    import availability
    import storage

    with tempfile.TemporaryDirectory() as tmp:
        conn = storage.init_db(str(Path(tmp) / "d.sqlite"))
        counts = seed(conn)
        assert counts["hours"] == 7, "1: every weekday is defined, including the closed one"

        # 2. CONFIGURABLE, NOT BAKED IN. a clinic that has set its own hours must
        # not have them overwritten by a re-seed - the same create-only rule
        # seed_users follows, and the reason the admin fixture drifted in P03.
        conn.execute("UPDATE clinic_hours SET opens = '08:00' WHERE weekday = 0")
        conn.commit()
        seed(conn)
        assert conn.execute("SELECT opens FROM clinic_hours WHERE weekday = 0").fetchone()[0] \
            == "08:00", "2: a configured schedule must survive a re-seed"
        conn.execute("UPDATE clinic_hours SET opens = '09:00' WHERE weekday = 0")
        conn.commit()

        # 3. and it is idempotent - no duplicate roster or closure rows
        before = conn.execute("SELECT COUNT(*) c FROM dentist_schedule").fetchone()["c"]
        seed(conn)
        assert conn.execute("SELECT COUNT(*) c FROM dentist_schedule").fetchone()["c"] == before, \
            "3: re-seeding must not duplicate the roster"

        # 4. THE FIXTURES DRIVE THE REAL RULE. these are the values every
        # scheduling test is written against, so they have to work through
        # availability.refusal rather than just existing in a table.
        MON, SAT, SUN = "2026-09-07", "2026-09-12", "2026-09-13"
        assert availability.refusal(conn, "dentist", f"{MON}T09:00", 30) is None, \
            "4: Monday morning is bookable"
        shut = availability.refusal(conn, "dentist", f"{SUN}T10:00", 30)
        assert shut and "does not open on Sundays" in shut, f"4: {shut}"
        late = availability.refusal(conn, "dentist", f"{SAT}T12:45", 30)
        assert late and "runs past closing" in late, \
            f"4: Saturday closes at 13:00 and the duration counts, got {late}"

        # 5. the part-timer's own window is narrower than the clinic's, which is
        # the case a uniform roster would never exercise
        early = availability.refusal(conn, "dr_ferrari", "2026-09-08T09:00", 30)
        assert early and "14:00-19:00" in early, f"5: {early}"
        assert availability.refusal(conn, "dr_ferrari", "2026-09-08T14:00", 30) is None, \
            "5: and inside it they are bookable"

        # 6. a public holiday closes a day the clinic would otherwise work, and
        # the reason reaches the user rather than a bare refusal
        holiday = availability.refusal(conn, "dentist", "2026-08-15T10:00", 30)
        assert holiday and "Ferragosto" in holiday, f"6: {holiday}"

        # 7. the summer shutdown is leave, not a closure - one dentist is away
        # while the clinic itself is open
        away = availability.refusal(conn, "dentist", "2026-08-12T10:00", 30)
        assert away and "chiusura estiva" in away, f"7: {away}"

        # 8. capacity is a whole-clinic limit. Saturday's is lower than the
        # week's, which is the point of setting it per weekday.
        sat = conn.execute("SELECT capacity FROM clinic_hours WHERE weekday = 5").fetchone()[0]
        week = conn.execute("SELECT capacity FROM clinic_hours WHERE weekday = 0").fetchone()[0]
        assert sat == 2 and week == 3, f"8: expected a reduced Saturday, got {sat} vs {week}"

        # 9. THE LABEL IS PART OF THE FIXTURE. these values must never be read
        # as a real clinic's hours.
        assert "DEMO FIXTURE" in LABEL and "not a real clinic" in LABEL.lower(), \
            "9: the fixtures must say what they are"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print(LABEL)


if __name__ == "__main__":
    main()
