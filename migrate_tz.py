"""Phase 52: convert stored naive clinic-local instants to UTC.

WHAT THIS CONVERTS, AND WHAT IT REFUSES TO TOUCH. The field audit is in
.planning/plans/P52.md section 1. Only class 1a (machine instants) and the
`starts_at` of BOOKED appointments are instants. Everything else is left alone
on purpose:

  * a REQUESTED appointment carries a preferred DATE and a period, and its time
    part is meaningless (PAPT-01). Converting it would invent a time the
    patient never chose.
  * clinic_hours and dentist_schedule hold civil time-of-day. "We open at
    09:00" is 09:00 in every season; shifting it by an offset is simply wrong.
  * visit dates, closures and absence ranges are dates.

THE SOURCE TIMEZONE IS A PARAMETER, NEVER A GUESS. D07 answered it for this
clinic on 2026-09-16 (Europe/Rome, with no period under another zone), and that
answer is what makes a conversion possible at all. It still has to be passed in
explicitly, because a database restored from somewhere else may not share it.

AMBIGUOUS AND NONEXISTENT LOCAL TIMES ARE REPORTED, NOT RESOLVED. 02:30 on the
fall-back Sunday is two real moments an hour apart. Picking one silently moves
an appointment, so those rows are listed for a person and left untouched.
"""

import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import clinic_time

# (table, column) pairs that hold a true instant. From P52 section 1a, plus the
# booked half of appointments.starts_at which is handled separately.
INSTANT_FIELDS = [
    ("audit_log", "ts"),
    ("sessions", "created_at"), ("sessions", "last_seen_at"),
    ("patient_sessions", "created_at"), ("patient_sessions", "last_seen_at"),
    ("patient_credentials", "issued_at"), ("patient_credentials", "expires_at"),
    ("patient_credentials", "locked_until"),
    ("patient_login_attempts", "attempted_at"),
    ("pending_actions", "created_at"),
    ("patient_merges", "merged_at"),
    ("patient_duplicate_dismissals", "dismissed_at"),
    ("users", "locked_until"),
    ("appointments", "created_at"), ("appointments", "updated_at"),
]

# Deliberately NOT converted. Listed so the preflight can say so out loud
# rather than leaving a reader to wonder whether they were missed.
LEFT_ALONE = [
    ("appointments", "starts_at", "status='requested' - a date and a period, never a time"),
    ("clinic_hours", "opens/closes", "civil time-of-day"),
    ("dentist_schedule", "starts/ends", "civil time-of-day"),
    ("visits", "visit_date", "date only"),
    ("clinic_closures", "closure_date", "date only"),
    ("dentist_absences", "from_date/to_date", "date only"),
]


def _classify(value, env):
    """-> ('ok', utc) | ('already_aware', None) | ('nonexistent'|'ambiguous', None)."""
    if not value:
        return "empty", None
    if clinic_time.has_offset(value):
        return "already_aware", None
    try:
        naive = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return "unparseable", None
    try:
        return "ok", clinic_time.to_utc(naive, env)
    except clinic_time.AmbiguousLocalTime as e:
        return ("nonexistent" if "does not exist" in str(e) else "ambiguous"), None


def preflight(conn, source_tz):
    """What the conversion WOULD do. Mutates nothing.

    A separate entry point rather than a flag inside the converter: a dry run
    that shares a code path with the real thing is one typo away from being it.
    """
    try:
        ZoneInfo(source_tz)
    except Exception:
        return {"blockers": [f"{source_tz!r} is not an IANA timezone this machine knows"]}

    env = {clinic_time.TZ_ENV: source_tz}
    report = {"source_tz": source_tz, "fields": {}, "unresolved": [],
              "left_alone": LEFT_ALONE, "blockers": []}

    for table, column in INSTANT_FIELDS:
        try:
            rows = conn.execute(
                f"SELECT rowid AS rid, {column} AS v FROM {table}"
                f" WHERE {column} IS NOT NULL AND {column} != ''").fetchall()
        except Exception:
            continue
        tally = {}
        for row in rows:
            state, _ = _classify(row["v"], env)
            tally[state] = tally.get(state, 0) + 1
            if state in ("ambiguous", "nonexistent", "unparseable"):
                report["unresolved"].append(
                    {"table": table, "column": column, "rowid": row["rid"],
                     "value": row["v"], "why": state})
        if tally:
            report["fields"][f"{table}.{column}"] = tally

    # booked appointments are the ones that carry a real instant
    try:
        booked = conn.execute(
            "SELECT id, starts_at FROM appointments WHERE status = 'booked'").fetchall()
        tally = {}
        for row in booked:
            state, utc = _classify(row["starts_at"], env)
            tally[state] = tally.get(state, 0) + 1
            if state in ("ambiguous", "nonexistent", "unparseable"):
                report["unresolved"].append(
                    {"table": "appointments", "column": "starts_at", "rowid": row["id"],
                     "value": row["starts_at"], "why": state})
        report["fields"]["appointments.starts_at (booked only)"] = tally
        report["preview"] = [
            {"id": r["id"], "from_local": r["starts_at"],
             "to_utc": _classify(r["starts_at"], env)[1].isoformat()
             if _classify(r["starts_at"], env)[0] == "ok" else None}
            for r in booked[:5]]
    except Exception:
        pass

    requested = conn.execute(
        "SELECT COUNT(*) c FROM appointments WHERE status = 'requested'").fetchone()["c"]
    report["requested_left_as_dates"] = requested
    if report["unresolved"]:
        report["blockers"].append(
            f"{len(report['unresolved'])} row(s) cannot be converted without a human deciding "
            "what they meant - see `unresolved`. Nothing has been changed.")
    return report


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        import migrate_tz_selftest
        migrate_tz_selftest.selftest()
        return
    print("usage: python migrate_tz.py --selftest")
    print("  the conversion itself is not wired to a CLI yet - preflight only")


if __name__ == "__main__":
    main()
