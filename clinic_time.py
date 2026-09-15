"""The clinic's clock, and what a stored datetime actually means.

THE CONTRACT, STATED ONCE SO IT STOPS BEING AN ACCIDENT:

    Every datetime stored by this project is NAIVE LOCAL TIME IN THE CLINIC'S
    OWN TIMEZONE.

That was already true before this module existed - every writer is
`datetime.now()` running on the clinic's machine - but it was true by habit
rather than by decision, which is the same thing as being one refactor away
from being false. Nothing here changes a stored value; it declares what they
mean and refuses to start if the data says otherwise.

WHY NOT UTC. P05.02 asks for timezone-aware instants. Storing UTC would change
what `appointments.agenda()`'s `day + "T00:00:00"` bounds and `month_counts`'s
`substr(starts_at, 1, 10)` mean on every read path, plus the patient portal and
the month calendar. Worse, it would keep working for realistic appointment
times and break silently for the rest, which is the least detectable kind of
wrong. The conversion is carried as its own phase (52); see
.planning/plans/P05.md section 3.

NO SPECULATIVE SHIFTING. A legacy naive value is never reinterpreted. If a row
ever turns up carrying an offset or a Z it was written under a different
contract, and guessing which one is how appointment times quietly move by an
hour. The guard refuses instead.
"""

import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import patient_id as _pidmod

TZ_ENV = "CLINIC_TZ"
# the whole product is Italian - the codice fiscale, the clinic yaml, the
# patient-facing strings. D07 (the owner's real timezone) is unanswered, so
# this is the recorded safe default, not a decision made on their behalf.
DEFAULT_TZ = "Europe/Rome"

LEGACY_ASSUMPTION = (
    "Stored datetimes are naive local time in the clinic's timezone. Every writer is "
    "datetime.now() on the clinic's own machine, so rows written before this contract was "
    "declared already satisfy it. No stored value has been shifted."
)


def zone_name(env=None):
    env = os.environ if env is None else env
    return (env.get(TZ_ENV) or DEFAULT_TZ).strip() or DEFAULT_TZ


def clinic_zone(env=None):
    name = zone_name(env)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"{TZ_ENV}={name!r} is not a timezone this machine knows")


def now(env=None):
    """The one clock. Naive, in the clinic's zone - see the module docstring."""
    return datetime.now(clinic_zone(env)).replace(tzinfo=None)


def parse(value):
    """A stored or posted datetime -> naive. Refuses anything carrying a zone.

    An aware value here is not a convenience to strip: it was produced under a
    different contract, and quietly dropping its offset moves the appointment.
    """
    if not isinstance(value, str):
        raise ValueError("a datetime must be text")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{value!r} is not a valid date and time")
    if parsed.tzinfo is not None:
        raise ValueError(
            f"{value!r} carries a timezone offset. Stored times are naive clinic-local "
            f"(see clinic_time.LEGACY_ASSUMPTION) - this value was written under a "
            f"different contract and must not be reinterpreted automatically.")
    return parsed


def has_offset(value):
    # cheap textual test for the guard, which runs over every stored row and
    # must not pay a full parse per row
    if not value or len(value) < 11:
        return False
    tail = value[10:]
    return tail.endswith("Z") or tail.endswith("z") or "+" in tail or "-" in tail


def is_dst_nonexistent(dt, env=None):
    """A local time that never happens - the hour DST skips forward over.

    Working hours put these out of reach in practice. The check exists so that
    stays true because a test says so, not because nobody tried.
    """
    tz = clinic_zone(env)
    aware = dt.replace(tzinfo=tz)
    # a skipped local time does not survive a round trip through UTC
    return aware.astimezone(ZoneInfo("UTC")).astimezone(tz).replace(tzinfo=None) != dt


def is_dst_ambiguous(dt, env=None):
    """A local time that happens twice - the hour DST repeats on fall-back."""
    tz = clinic_zone(env)
    first = dt.replace(tzinfo=tz, fold=0)
    second = dt.replace(tzinfo=tz, fold=1)
    return first.utcoffset() != second.utcoffset()


def contract_refusal(conn):
    """Why the apps must not start, or None. Pure enough to test every branch.

    Reads the columns that hold instants. A single row carrying an offset means
    something wrote under a different contract, and the honest response is to
    stop rather than to guess what the value meant.
    """
    try:
        clinic_zone()
    except ValueError as e:
        return str(e)

    for table, column in (("appointments", "starts_at"),
                          ("appointments", "created_at"),
                          ("visits", "visit_date")):
        try:
            rows = conn.execute(
                f"SELECT {column} AS v FROM {table} WHERE {column} IS NOT NULL").fetchall()
        except Exception:
            continue     # a fresh database may not have the table yet
        for row in rows:
            if has_offset(row["v"]):
                return (f"{table}.{column} holds {row['v']!r}, which carries a timezone offset. "
                        f"{LEGACY_ASSUMPTION} A value written under a different contract must be "
                        f"migrated deliberately, not reinterpreted at startup.")
    return None


def guard_or_exit(db_path):
    # startup guard, sibling of disk_guard and codice_fiscale.guard_or_exit
    import sqlite3
    from pathlib import Path
    if not Path(db_path).exists():
        return
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        reason = contract_refusal(conn)
    finally:
        conn.close()
    if reason:
        print(f"refusing to start: {reason}", file=sys.stderr)
        sys.exit(1)


def selftest():
    import sqlite3
    import tempfile
    from pathlib import Path

    import storage

    # 1. the default is recorded, and an override is honoured
    assert zone_name({}) == DEFAULT_TZ, "1: the default clinic zone is Europe/Rome"
    assert zone_name({TZ_ENV: "Europe/Paris"}) == "Europe/Paris", "1: the env wins"
    assert zone_name({TZ_ENV: "   "}) == DEFAULT_TZ, "1: a blank override falls back"
    try:
        clinic_zone({TZ_ENV: "Mars/Olympus"})
        raise AssertionError("1: an unknown zone must be refused, not silently ignored")
    except ValueError:
        pass

    # 2. now() is naive - an aware value would propagate into every stored
    # column the moment a caller used it
    assert now().tzinfo is None, "2: the clinic clock is naive by contract"

    # 3. parse() REFUSES an aware value rather than stripping it. stripping is
    # the bug: it moves the appointment by the offset and says nothing.
    assert parse("2026-09-07T09:00").hour == 9
    assert parse("2026-09-07T09:00:00").minute == 0
    for aware in ("2026-09-07T09:00:00Z", "2026-09-07T09:00:00+02:00",
                  "2026-09-07T09:00:00-05:00"):
        try:
            parse(aware)
            raise AssertionError(f"3: {aware} carries a zone and must be refused")
        except ValueError as e:
            assert "different contract" in str(e), "3: and must say why"
    for bad in ("not a date", "", None, 5):
        try:
            parse(bad)
            raise AssertionError(f"3: {bad!r} must be refused")
        except ValueError:
            pass

    # 4. the cheap textual test agrees with the parser, including on the
    # hyphens inside a date (the trap: 2026-09-07 is full of '-')
    assert not has_offset("2026-09-07T09:00:00"), "4: a plain local value has no offset"
    assert not has_offset("2026-09-07"), "4: nor does a bare date"
    assert has_offset("2026-09-07T09:00:00Z"), "4: Z is an offset"
    assert has_offset("2026-09-07T09:00:00+02:00"), "4: and so is +02:00"
    assert has_offset("2026-09-07T09:00:00-05:00"), \
        "4: a trailing -05:00 must not be mistaken for the date's hyphens"

    # 5. DST. Europe/Rome springs forward at 02:00 on the last Sunday in March
    # and falls back at 03:00 on the last Sunday in October.
    assert is_dst_nonexistent(datetime(2026, 3, 29, 2, 30)), \
        "5: 02:30 on the spring-forward day does not exist"
    assert not is_dst_nonexistent(datetime(2026, 3, 29, 4, 30)), "5: 04:30 that day does"
    assert is_dst_ambiguous(datetime(2026, 10, 25, 2, 30)), \
        "5: 02:30 on the fall-back day happens twice"
    assert not is_dst_ambiguous(datetime(2026, 10, 25, 4, 30)), "5: 04:30 happens once"
    assert not is_dst_nonexistent(datetime(2026, 9, 7, 9, 0)), \
        "5: an ordinary working hour is neither"

    with tempfile.TemporaryDirectory() as tmp:
        conn = storage.init_db(str(Path(tmp) / "t.sqlite"))
        tpid = _pidmod.seed_patient(conn, "AAAA000000000001", "Time Patient")
        conn.commit()

        # 6. today's data satisfies the contract, so the guard lets the apps up
        conn.execute(
            "INSERT INTO appointments (patient_id, dentist, starts_at, minutes,"
            " status, created_at, updated_at) VALUES"
            " (?,'dr rossi','2026-09-07T09:00:00',30,'booked',"
            " '2026-09-07T08:00:00','2026-09-07T08:00:00')", (tpid,))
        conn.commit()
        assert contract_refusal(conn) is None, \
            "6: naive clinic-local rows are exactly what the contract expects"

        # 7. AND IT REFUSES RATHER THAN SHIFTING. this is the whole point: a
        # value written under another contract is migrated deliberately or not
        # at all, because a silent reinterpretation moves appointments by an
        # hour and leaves no trace.
        conn.execute(
            "INSERT INTO appointments (patient_id, dentist, starts_at, minutes,"
            " status, created_at, updated_at) VALUES"
            " (?,'dr rossi','2026-09-08T09:00:00+02:00',30,'booked',"
            " '2026-09-08T08:00:00','2026-09-08T08:00:00')", (tpid,))
        conn.commit()
        reason = contract_refusal(conn)
        assert reason and "+02:00" in reason, f"7: the offending value must be named, got {reason}"
        assert "not reinterpreted" in reason, "7: and the refusal must say why"

        # 8. the guard reads; it must never write. a startup check that repairs
        # data is a migration nobody asked for.
        before = conn.execute("SELECT starts_at FROM appointments ORDER BY id").fetchall()
        contract_refusal(conn)
        after = conn.execute("SELECT starts_at FROM appointments ORDER BY id").fetchall()
        assert [r["starts_at"] for r in before] == [r["starts_at"] for r in after], \
            "8: the contract guard must not modify a single stored value"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python clinic_time.py --selftest")


if __name__ == "__main__":
    main()
