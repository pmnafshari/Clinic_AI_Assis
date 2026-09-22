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

import sqlite3
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import clinic_time

MIGRATION = "tz_utc"
PENDING, DONE, FAILED = "pending", "done", "failed"

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


def _ledger(conn):
    import migrate_pid
    conn.executescript(migrate_pid.OPS_SCHEMA)
    conn.commit()


def _record(conn, step, subject, state, detail=None):
    conn.execute(
        "INSERT OR REPLACE INTO migration_ops"
        " (migration, step, subject, state, payload, detail, attempts, updated_at)"
        " VALUES (?, ?, ?, ?, NULL, ?,"
        "   COALESCE((SELECT attempts FROM migration_ops WHERE migration = ? AND step = ?"
        "             AND subject = ?), 0) + 1, ?)",
        (MIGRATION, step, subject, state, detail, MIGRATION, step, subject,
         clinic_time.stamp()))


def already_done(conn, step, subject):
    try:
        row = conn.execute(
            "SELECT state FROM migration_ops WHERE migration = ? AND step = ? AND subject = ?",
            (MIGRATION, step, subject)).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None and row["state"] == DONE


def convert(conn, source_tz, backup_path=None):
    """Convert every stored instant from `source_tz` local text to UTC.

    THE RULES THIS KEEPS, IN THE ORDER THEY MATTER.

      * A BACKUP IS MANDATORY. `backup_path` must name a file that exists. This
        rewrites the meaning of every timestamp in the database and there is no
        inverse that can tell a converted row from one that was always UTC, so
        "run it again backwards" is not a recovery plan.
      * NOTHING MOVES IF ANYTHING IS UNRESOLVED. The preflight runs first and a
        single ambiguous or nonexistent local time aborts the whole run before
        a byte changes. 02:30 on the fall-back Sunday is two real moments an
        hour apart; converting the rest and leaving that one is how a database
        ends up half in each contract.
      * IDEMPOTENT. An already-aware value is skipped, not converted twice - a
        second run over a converted database is a no-op, and a run interrupted
        halfway can simply be repeated.
      * ONE TRANSACTION. Either every instant is UTC or none of them are.
      * DATE-ONLY AND CIVIL-TIME FIELDS ARE NEVER TOUCHED, and the report says
        so out loud rather than leaving a reader to wonder.
    """
    if not backup_path:
        return {"ok": False, "blockers": [
            "a backup is mandatory before converting. this rewrites what every stored "
            "timestamp means, and no inverse can distinguish a converted row from one that "
            "was always UTC."]}
    from pathlib import Path
    if not Path(backup_path).exists():
        return {"ok": False, "blockers": [f"the backup {backup_path!r} does not exist"]}

    report = preflight(conn, source_tz)
    if report.get("blockers"):
        return {"ok": False, "blockers": report["blockers"],
                "unresolved": report.get("unresolved", []),
                "note": "nothing has been changed"}

    env = {clinic_time.TZ_ENV: source_tz}
    _ledger(conn)
    changed = {}
    skipped = {}

    try:
        conn.execute("BEGIN")
        # audit_log.ts is one of the fields, and the audit trail is append-only.
        # a backed-up migration is the one other thing allowed through; the
        # unlock is inside this transaction, so a failure rolls it back too
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'audit_unlock'").fetchone():
            conn.execute("INSERT INTO audit_unlock (reason) VALUES ('migrate_tz')")
        for table, column in INSTANT_FIELDS:
            subject = f"{table}.{column}"
            if already_done(conn, "field", subject):
                skipped[subject] = "already done"
                continue
            try:
                rows = conn.execute(
                    f"SELECT rowid AS rid, {column} AS v FROM {table}"
                    f" WHERE {column} IS NOT NULL AND {column} != ''").fetchall()
            except sqlite3.OperationalError:
                continue        # the table is not in this database
            n = 0
            for row in rows:
                state, utc = _classify(row["v"], env)
                if state != "ok":
                    continue    # already_aware and empty are both no-ops
                conn.execute(f"UPDATE {table} SET {column} = ? WHERE rowid = ?",
                             (clinic_time.to_storage(utc), row["rid"]))
                n += 1
            changed[subject] = n
            _record(conn, "field", subject, DONE, f"{n} row(s) converted")

        # BOOKED ONLY. a requested row's starts_at is a date and a period; its
        # time part is meaningless (PAPT-01) and converting it would invent an
        # hour the patient never chose.
        subject = "appointments.starts_at(booked)"
        if already_done(conn, "field", subject):
            skipped[subject] = "already done"
        else:
            n = 0
            for row in conn.execute(
                    "SELECT id, starts_at FROM appointments WHERE status = 'booked'").fetchall():
                state, utc = _classify(row["starts_at"], env)
                if state != "ok":
                    continue
                conn.execute("UPDATE appointments SET starts_at = ? WHERE id = ?",
                             (clinic_time.to_storage(utc), row["id"]))
                n += 1
            changed[subject] = n
            _record(conn, "field", subject, DONE, f"{n} row(s) converted")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'audit_unlock'").fetchone():
            conn.execute("DELETE FROM audit_unlock")
        conn.commit()
    except Exception as e:
        conn.rollback()
        try:
            _record(conn, "field", "run", FAILED, str(e)[:200])
            conn.commit()
        except Exception:
            pass
        return {"ok": False, "blockers": [f"conversion failed and was rolled back: {e}"],
                "note": "the database is unchanged"}

    return {"ok": True, "source_tz": source_tz, "converted": changed, "skipped": skipped,
            "left_alone": LEFT_ALONE,
            "requested_left_as_dates": report.get("requested_left_as_dates", 0),
            "backup": str(backup_path)}


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        import migrate_tz_selftest
        migrate_tz_selftest.selftest()
        return
    if len(sys.argv) < 4 or sys.argv[1] not in ("--preflight", "--convert"):
        print("usage: python migrate_tz.py --selftest")
        print("       python migrate_tz.py --preflight <db> <source-tz>")
        print("       python migrate_tz.py --convert <db> <source-tz> --backup <file>")
        return

    mode, db_path, source_tz = sys.argv[1], sys.argv[2], sys.argv[3]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if mode == "--preflight":
            report = preflight(conn, source_tz)
            print(f"source timezone: {report.get('source_tz')}")
            for field, tally in report.get("fields", {}).items():
                print(f"  {field}: {tally}")
            print(f"  requests left as dates: {report.get('requested_left_as_dates')}")
            for table, column, why in report.get("left_alone", []):
                print(f"  NOT converted: {table}.{column} - {why}")
            for row in report.get("unresolved", []):
                print(f"  UNRESOLVED {row['table']}.{row['column']} #{row['rowid']}:"
                      f" {row['value']} ({row['why']})")
            print("blockers:", report.get("blockers") or "none")
        elif mode == "--convert":
            backup = sys.argv[sys.argv.index("--backup") + 1] if "--backup" in sys.argv else None
            result = convert(conn, source_tz, backup_path=backup)
            if not result["ok"]:
                for blocker in result["blockers"]:
                    print(f"refused: {blocker}", file=sys.stderr)
                sys.exit(1)
            for field, n in result["converted"].items():
                print(f"  {field}: {n} converted")
            for field, why in result.get("skipped", {}).items():
                print(f"  {field}: {why}")
            print(f"  requests left as dates: {result['requested_left_as_dates']}")
            print("done")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
