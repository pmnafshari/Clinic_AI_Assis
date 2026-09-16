"""The clinic's clock, and what a stored datetime actually means.

THE CONTRACT, STATED ONCE SO IT STOPS BEING AN ACCIDENT. Phase 52 replaced the
P05 contract; there are now three kinds of stored temporal value and they are
not interchangeable:

    1. INSTANTS are stored as UTC-AWARE ISO text ("...+00:00"). A point in
       time: when a row was written, when a session was last seen, and the
       start of a BOOKED appointment. Rendered in clinic-local time, never
       stored that way.

    2. CIVIL TIME-OF-DAY is stored as a bare "HH:MM" and is NEVER converted.
       "We open at 09:00" is 09:00 in January and in July. Opening hours and
       roster windows are this.

    3. DATES are stored as a bare "YYYY-MM-DD" and are NEVER converted. A
       visit day, a holiday, a leave range - and the `starts_at` of a
       REQUESTED appointment, whose time part is meaningless by construction
       (PAPT-01).

WHY UTC, AND WHAT IT COST. P05 stored naive clinic-local text and said so out
loud, because converting would change what `agenda()`'s day bounds and
`month_counts`'s `substr(starts_at, 1, 10)` meant on every read path. That was
true, and Phase 52 is the phase that paid it: no read path bounds a local day
by slicing stored text any more. Day-bounded queries go through
`day_bounds_utc()` and the exact day is decided by `local_date()`, because a
local day is not a UTC day and slicing a UTC string to ten characters is the
version of this that looks right and is wrong for an hour either side.

NO SPECULATIVE SHIFTING, STILL. `read_instant` REFUSES a naive value rather
than assuming it meant clinic-local. A naive instant after Phase 52 is an
unmigrated row, and quietly reinterpreting it is how appointment times move by
an hour with nothing in the log. `migrate_tz` converts deliberately, with the
source timezone passed in by a person.
"""

import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import patient_id as _pidmod

TZ_ENV = "CLINIC_TZ"

# A FIXTURE, NOT A PRODUCTION DEFAULT (Phase 52). D07 named Europe/Rome as this
# clinic's real zone on 2026-09-16, and that belongs in CLINIC_TZ in the
# deployment - not baked in here. Production refuses to start without an
# explicit valid IANA identifier, and NEVER falls back to the machine's zone:
# a server moved to another region would silently reinterpret every appointment.
FIXTURE_TZ = "Europe/Rome"
DEFAULT_TZ = FIXTURE_TZ          # development convenience only; see is_production

LEGACY_ASSUMPTION = (
    "Stored datetimes are naive local time in the clinic's timezone. Every writer is "
    "datetime.now() on the clinic's own machine, so rows written before this contract was "
    "declared already satisfy it. No stored value has been shifted."
)


def zone_name(env=None):
    """The configured zone, or the dev fixture. Production must be explicit."""
    env = os.environ if env is None else env
    configured = (env.get(TZ_ENV) or "").strip()
    if configured:
        return configured
    import codice_fiscale
    if codice_fiscale.is_production(env):
        raise ValueError(
            f"{TZ_ENV} is not set. Production must name the clinic's IANA timezone "
            f"explicitly (for example CLINIC_TZ=Europe/Rome) - it is never inferred from "
            f"the machine, the language or the country.")
    return DEFAULT_TZ


UTC = ZoneInfo("UTC")


def now_utc():
    """The one instant clock. Aware UTC - every stored instant starts here."""
    return datetime.now(UTC)


def stamp():
    """Storage text for "now". What every writer of a class-1a field calls."""
    return now_utc().isoformat()


def to_storage(instant):
    """An aware instant -> canonical stored text.

    Always UTC, always with the offset, so string comparison in SQL orders
    instants correctly - the property the ISO-text columns were chosen for.
    """
    if instant.tzinfo is None:
        raise ValueError("to_storage takes an aware instant, not a naive value")
    return instant.astimezone(UTC).isoformat()


def read_instant(value):
    """Stored text -> an aware UTC instant. REFUSES a naive value.

    A naive instant after Phase 52 has not been migrated. Reading it as
    clinic-local here would be a silent conversion in the read path, which is
    the thing this whole phase exists to stop happening by accident.
    """
    if not isinstance(value, str):
        raise ValueError("a stored instant must be text")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{value!r} is not a valid date and time")
    if parsed.tzinfo is None:
        raise ValueError(
            f"{value!r} is naive. Stored instants are UTC-aware since Phase 52 - this row "
            f"has not been migrated, and reinterpreting it on a read path would move it "
            f"silently. Run migrate_tz.")
    return parsed.astimezone(UTC)


def local_of(value, env=None):
    """Stored instant text -> naive clinic-local wall time, for display."""
    return to_local(read_instant(value), env)


def local_date(value, env=None):
    """The clinic-local DATE an instant falls on. The only correct way to ask.

    Slicing the stored text to ten characters asks a UTC question and is wrong
    by up to an offset either side of midnight.
    """
    return local_of(value, env).date().isoformat()


def local_hhmm(value, env=None):
    return local_of(value, env).strftime("%H:%M")


def to_utc_text(local_naive, env=None):
    """Clinic-local wall time -> stored text. The booking write path."""
    return to_storage(to_utc(local_naive, env))


def day_bounds_utc(day, env=None):
    """A clinic-local date -> (lo, hi) stored-text bounds covering that day.

    NEVER NARROWER THAN THE LOCAL DAY. On a DST boundary local midnight can be
    ambiguous or skipped, so the lower bound takes the earlier of the two
    candidate offsets and the upper bound the later. The window can therefore
    be up to an hour wide at the edges, and a caller that needs the exact day
    filters on `local_date()`. Wider and then exact is recoverable; narrower is
    a booking that vanishes from the day it is on.
    """
    if isinstance(day, str):
        parsed = date.fromisoformat(day)
    else:
        parsed = day
    tz = clinic_zone(env)

    def midnight(d, pick):
        naive = datetime(d.year, d.month, d.day)
        a = naive.replace(tzinfo=tz, fold=0).astimezone(UTC)
        b = naive.replace(tzinfo=tz, fold=1).astimezone(UTC)
        return min(a, b) if pick == "early" else max(a, b)

    lo = midnight(parsed, "early")
    hi = midnight(parsed + timedelta(days=1), "late")
    return lo.isoformat(), hi.isoformat()


def to_utc(local_naive, env=None):
    """Clinic-local wall time -> an aware UTC instant.

    Refuses a time that does not exist (the hour DST skips) and one that
    happens twice (the hour DST repeats) rather than picking a side. Both are
    a question for a person: 02:30 on the fall-back Sunday is two different
    real moments an hour apart.
    """
    if local_naive.tzinfo is not None:
        raise ValueError("to_utc takes a naive clinic-local time, not an aware one")
    if is_dst_nonexistent(local_naive, env):
        raise AmbiguousLocalTime(
            f"{local_naive.isoformat()} does not exist in {zone_name(env)} - the clocks go forward")
    if is_dst_ambiguous(local_naive, env):
        raise AmbiguousLocalTime(
            f"{local_naive.isoformat()} happens twice in {zone_name(env)} - the clocks go back")
    return local_naive.replace(tzinfo=clinic_zone(env)).astimezone(ZoneInfo("UTC"))


def to_local(instant, env=None):
    """An aware UTC instant -> naive clinic-local wall time, for display."""
    if instant.tzinfo is None:
        raise ValueError("to_local takes an aware instant, not a naive value")
    return instant.astimezone(clinic_zone(env)).replace(tzinfo=None)


def same_date(value):
    """A date-only value passes through untouched.

    Appointment REQUESTS, visit dates, closures and absence ranges are dates,
    not instants (P52 §1c). Converting one would invent a time nobody chose -
    a request carries a preferred day and a period, and that is all it means.
    """
    if not isinstance(value, str) or len(value) != 10 or value[4] != "-":
        raise ValueError(f"{value!r} is not a bare YYYY-MM-DD date")
    return value


class AmbiguousLocalTime(ValueError):
    """A local time that does not exist, or that happens twice."""


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


def _rows(conn, sql, params=()):
    try:
        return conn.execute(sql, params).fetchall()
    except Exception:
        return []        # a fresh database may not have the table yet


def contract_refusal(conn):
    """Why the apps must not start, or None. Pure enough to test every branch.

    THE SENSE OF THIS CHECK INVERTED AT PHASE 52. Under P05 an offset meant a
    row written under a foreign contract and the guard refused it. Now an
    instant MUST carry one, and a naive instant is the unmigrated row - so the
    guard refuses that instead, and says to run the migration rather than
    letting the apps read it as though it were already UTC.

    The three kinds are checked apart because they disagree on purpose: a
    BOOKED appointment is an instant, a REQUESTED one is a date marker, and a
    visit date is a date. A guard that checked them together would have to pick
    one answer and be wrong about two of them.
    """
    try:
        clinic_zone()
    except ValueError as e:
        return str(e)

    for row in _rows(conn, "SELECT starts_at AS v FROM appointments"
                           " WHERE status = 'booked' AND starts_at IS NOT NULL"):
        if not has_offset(row["v"]):
            return (f"appointments.starts_at holds {row['v']!r} on a booked row, with no timezone "
                    f"offset. Stored instants are UTC-aware since Phase 52; this row predates the "
                    f"conversion. Run migrate_tz rather than letting a read path guess what it "
                    f"meant.")

    for row in _rows(conn, "SELECT created_at AS v FROM appointments WHERE created_at IS NOT NULL"):
        if not has_offset(row["v"]):
            return (f"appointments.created_at holds {row['v']!r}, with no timezone offset. "
                    f"It is a machine instant and must be UTC-aware. Run migrate_tz.")

    # and the two that must NOT move. a converted request would carry an hour
    # the patient never chose (PAPT-01); a converted visit date would become an
    # instant, and the day it belongs to would depend on the reader's offset.
    for row in _rows(conn, "SELECT starts_at AS v FROM appointments"
                           " WHERE status = 'requested' AND starts_at IS NOT NULL"):
        if has_offset(row["v"]):
            return (f"appointments.starts_at holds {row['v']!r} on a REQUESTED row. A request "
                    f"carries a preferred date and a period, never a time (PAPT-01) - an offset "
                    f"here means something converted a value that has no time to convert.")

    for row in _rows(conn, "SELECT visit_date AS v FROM visits WHERE visit_date IS NOT NULL"):
        if has_offset(row["v"]):
            return (f"visits.visit_date holds {row['v']!r}. A visit date is a date, not an "
                    f"instant, and must never carry an offset.")

    # a cheap canary on the largest class-1a table: one row, not a scan
    newest = _rows(conn, "SELECT MAX(ts) AS v FROM audit_log")
    if newest and newest[0]["v"] and not has_offset(newest[0]["v"]):
        return (f"audit_log.ts holds {newest[0]['v']!r}, with no timezone offset. Machine "
                f"instants are UTC-aware since Phase 52. Run migrate_tz.")
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

        # 6. a MIGRATED row satisfies the contract, so the guard lets the apps
        # up. the booked start is an aware instant; a requested row beside it
        # stays a bare date marker and must not be mistaken for one.
        conn.execute(
            "INSERT INTO appointments (patient_id, dentist, starts_at, minutes,"
            " status, created_at, updated_at) VALUES"
            " (?,'dr rossi','2026-09-07T07:00:00+00:00',30,'booked',"
            " '2026-09-07T06:00:00+00:00','2026-09-07T06:00:00+00:00')", (tpid,))
        conn.execute(
            "INSERT INTO appointments (patient_id, dentist, starts_at, minutes,"
            " status, period, created_at, updated_at) VALUES"
            " (?,'',  '2026-09-09T00:00:00',0,'requested','morning',"
            " '2026-09-07T06:00:00+00:00','2026-09-07T06:00:00+00:00')", (tpid,))
        conn.commit()
        assert contract_refusal(conn) is None, \
            f"6: migrated rows are what the contract expects, got {contract_refusal(conn)}"

        # 7. THE SENSE OF THIS INVERTED AT PHASE 52. a naive booked start is now
        # the unmigrated row, and reading it as though it were already UTC would
        # move the appointment by an offset with nothing in the log. the guard
        # names the row and says to run the migration.
        conn.execute(
            "INSERT INTO appointments (patient_id, dentist, starts_at, minutes,"
            " status, created_at, updated_at) VALUES"
            " (?,'dr rossi','2026-09-08T09:00:00',30,'booked',"
            " '2026-09-08T06:00:00+00:00','2026-09-08T06:00:00+00:00')", (tpid,))
        conn.commit()
        reason = contract_refusal(conn)
        assert reason and "2026-09-08T09:00:00" in reason, \
            f"7: the offending value must be named, got {reason}"
        assert "migrate_tz" in reason, "7: and the refusal must say what to run"
        conn.execute("DELETE FROM appointments WHERE starts_at = '2026-09-08T09:00:00'")
        conn.commit()

        # 7b. AND A CONVERTED REQUEST IS REFUSED TOO. a request carries a date
        # and a period; an offset on one means something converted a value that
        # has no time to convert, inventing an hour the patient never chose.
        conn.execute(
            "INSERT INTO appointments (patient_id, dentist, starts_at, minutes,"
            " status, period, created_at, updated_at) VALUES"
            " (?,'','2026-09-10T00:00:00+02:00',0,'requested','morning',"
            " '2026-09-07T06:00:00+00:00','2026-09-07T06:00:00+00:00')", (tpid,))
        conn.commit()
        reason = contract_refusal(conn)
        assert reason and "PAPT-01" in reason, f"7b: a converted request must be refused, got {reason}"
        conn.execute("DELETE FROM appointments WHERE status = 'requested'")
        conn.commit()

        # 8. the guard reads; it must never write. a startup check that repairs
        # data is a migration nobody asked for.
        before = conn.execute("SELECT starts_at FROM appointments ORDER BY id").fetchall()
        contract_refusal(conn)
        after = conn.execute("SELECT starts_at FROM appointments ORDER BY id").fetchall()
        assert [r["starts_at"] for r in before] == [r["starts_at"] for r in after], \
            "8: the contract guard must not modify a single stored value"

    # --- Phase 52: config, conversion, DST, date-only ------------------
    from datetime import datetime as _dt

    # 9. PRODUCTION MUST BE EXPLICIT, and must never inherit the machine's zone.
    # a server moved to another region would otherwise reinterpret every stored
    # appointment silently.
    try:
        zone_name({"CLINIC_ENV": "production"})
        raise AssertionError("9: production with no CLINIC_TZ must refuse")
    except ValueError as e:
        assert TZ_ENV in str(e), "9: and must name the variable it needs"
    assert zone_name({"CLINIC_ENV": "production", TZ_ENV: "Europe/Rome"}) == "Europe/Rome", \
        "9: production with a valid zone starts"
    try:
        clinic_zone({"CLINIC_ENV": "production", TZ_ENV: "Mars/Olympus"})
        raise AssertionError("9: an invalid IANA identifier must be refused")
    except ValueError:
        pass
    assert zone_name({}) == FIXTURE_TZ, "9: development still has a fixture to work with"

    # 10. round trip, in both directions
    ROME = {TZ_ENV: "Europe/Rome"}
    winter, summer = _dt(2026, 1, 15, 9, 0), _dt(2026, 7, 15, 9, 0)
    assert to_utc(winter, ROME).isoformat() == "2026-01-15T08:00:00+00:00", "10: CET is +1"
    assert to_utc(summer, ROME).isoformat() == "2026-07-15T07:00:00+00:00", "10: CEST is +2"
    for local in (winter, summer, _dt(2026, 12, 31, 23, 30), _dt(2027, 1, 1, 0, 30)):
        assert to_local(to_utc(local, ROME), ROME) == local, f"10: round trip failed for {local}"
    try:
        to_utc(to_utc(winter, ROME), ROME)
        raise AssertionError("10: to_utc must refuse an already-aware value")
    except ValueError:
        pass
    try:
        to_local(winter, ROME)
        raise AssertionError("10: to_local must refuse a naive value")
    except ValueError:
        pass

    # 11. DST. NEITHER IS RESOLVED BY GUESSING - 02:30 on the fall-back Sunday
    # is two real moments an hour apart, and picking one silently moves an
    # appointment.
    try:
        to_utc(_dt(2026, 3, 29, 2, 30), ROME)
        raise AssertionError("11: a nonexistent local time must be refused")
    except AmbiguousLocalTime as e:
        assert "does not exist" in str(e)
    try:
        to_utc(_dt(2026, 10, 25, 2, 30), ROME)
        raise AssertionError("11: an ambiguous local time must be refused")
    except AmbiguousLocalTime as e:
        assert "twice" in str(e)
    # the hours either side are ordinary and must still convert
    for fine in (_dt(2026, 3, 29, 4, 30), _dt(2026, 10, 25, 4, 30)):
        assert to_local(to_utc(fine, ROME), ROME) == fine, f"11: {fine} is an ordinary hour"

    # 12. A DATE IS NOT AN INSTANT. an appointment REQUEST carries a preferred
    # day and a period (PAPT-01); converting it would invent a time the patient
    # never chose.
    assert same_date("2026-09-07") == "2026-09-07", "12: a date passes through untouched"
    for bad in ("2026-09-07T09:00", "07/09/2026", "", None, 20260907):
        try:
            same_date(bad)
            raise AssertionError(f"12: {bad!r} is not a bare date and must be refused")
        except ValueError:
            pass

    # 13. the instant layer. a stored instant is UTC-aware text, and reading
    # one REFUSES a naive value rather than assuming it meant clinic-local.
    assert stamp().endswith("+00:00"), "13: a stamp is UTC-aware"
    assert now_utc().tzinfo is not None, "13: the instant clock is aware"
    assert read_instant("2026-07-15T07:00:00+00:00").hour == 7, "13: read back as UTC"
    assert read_instant("2026-07-15T09:00:00+02:00").hour == 7, \
        "13: a non-UTC offset is normalised, not dropped"
    for naive in ("2026-07-15T09:00:00", "2026-07-15T09:00"):
        try:
            read_instant(naive)
            raise AssertionError(f"13: {naive} is unmigrated and must be refused")
        except ValueError as e:
            assert "migrate_tz" in str(e), "13: and must say what to run"
    try:
        to_storage(_dt(2026, 7, 15, 9, 0))
        raise AssertionError("13: to_storage must refuse a naive value")
    except ValueError:
        pass

    # 14. DISPLAY IS LOCAL. the round trip a booking makes: local wall time in,
    # stored UTC, local wall time back out - in both seasons.
    for local, stored in ((_dt(2026, 1, 15, 9, 0), "2026-01-15T08:00:00+00:00"),
                          (_dt(2026, 7, 15, 9, 0), "2026-07-15T07:00:00+00:00")):
        assert to_utc_text(local, ROME) == stored, f"14: {local} stores as {stored}"
        assert local_of(stored, ROME) == local, "14: and renders back to the same wall time"
        assert local_hhmm(stored, ROME) == "09:00", "14: the clinic sees 09:00 in both seasons"

    # 15. A LOCAL DAY IS NOT A UTC DAY. this is the one that makes slicing
    # stored text to ten characters wrong: 00:30 local on the 16th is still the
    # 15th in UTC, and an agenda that sliced would file it under the wrong day.
    late = to_utc_text(_dt(2026, 7, 16, 0, 30), ROME)
    assert late[:10] == "2026-07-15", "15: the UTC text says the 15th"
    assert local_date(late, ROME) == "2026-07-16", "15: the clinic's day is the 16th"
    lo, hi = day_bounds_utc("2026-07-16", ROME)
    assert lo <= late < hi, "15: and the day bounds must contain it"

    # 16. THE BOUNDS ARE NEVER NARROWER THAN THE LOCAL DAY, including across
    # both DST boundaries, where the local day is 23 or 25 hours long. a
    # narrow window is a booking that disappears from the day it is on.
    for day, hours in (("2026-03-29", 23), ("2026-10-25", 25), ("2026-07-15", 24)):
        lo, hi = day_bounds_utc(day, ROME)
        span = (datetime.fromisoformat(hi) - datetime.fromisoformat(lo)).total_seconds() / 3600
        assert span >= hours, f"16: {day} is {hours}h long, bounds span {span}h"
        # every hour the clinic could actually book that day lands inside
        for hour in range(8, 20):
            stored = to_utc_text(_dt(int(day[:4]), int(day[5:7]), int(day[8:]), hour, 0), ROME)
            assert lo <= stored < hi, f"16: {day} {hour}:00 must fall inside its own day"
            assert local_date(stored, ROME) == day, f"16: and report {day} as its local date"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python clinic_time.py --selftest")


if __name__ == "__main__":
    main()
