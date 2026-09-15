"""Phase 51: move patient identity off the codice fiscale onto a surrogate id.

NO DISTRIBUTED ATOMICITY IS CLAIMED, AND NONE IS AVAILABLE. This migration
touches three stores that cannot share a transaction: SQLite, the Chroma index
and the filesystem. What it provides instead is a RECOVERABLE STATE MACHINE:

  * SQLite moves first, in one transaction, and in the same transaction it
    enqueues one `chroma` and one `filesystem` operation per patient into
    `migration_ops`.
  * Each later step processes pending rows and marks its own done.
  * An interruption anywhere leaves pending rows behind. Re-running continues
    from them; every step is idempotent.

So the honest guarantee is: after any failure the system is in a state that is
recorded, resumable and never half-attributes data to the wrong patient - not
that the three stores move as one.

WHY THE CODICE FISCALE HAD TO STOP BEING THE KEY. It was the primary key, every
foreign key, the Chroma metadata key, the `sorted/<CF>/` directory name and the
URL. It is also personal data - it encodes a surname, a first name, a birth date
and a birthplace - so it was simultaneously the value handed to any future
payment or scheduling integration. And because it was identity, correcting a
mistyped one meant rewriting every relation.
"""

import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import patient_id

MIGRATION = "p51_surrogate_id"
STEP_CHROMA = "chroma"
STEP_FS = "filesystem"
PENDING, DONE, FAILED = "pending", "done", "failed"

OPS_SCHEMA = """
CREATE TABLE IF NOT EXISTS migration_ops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    migration TEXT NOT NULL,
    step TEXT NOT NULL,
    subject TEXT NOT NULL,
    state TEXT NOT NULL,
    -- WHAT THE RETRY NEEDS, kept apart from WHAT HAPPENED. these were one
    -- column once: marking an op failed overwrote the codice fiscale the retry
    -- reads with the error message, so the retry looked for a directory named
    -- after the error, found nothing, and marked itself done - ledger complete,
    -- directory never migrated. Found by the failure-injection test, which is
    -- the only thing that would have found it.
    payload TEXT,
    detail TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    UNIQUE(migration, step, subject)
);
"""

# The v2 shape. `codice_fiscale` is GONE from every child table on purpose: a
# second copy of identity beside the real one is what lets the two drift, and
# the drift is invisible until somebody reads the stale copy.
# ONE DEFINITION PER TABLE. The migration renders these as `<name>_v2` while it
# copies, and a fresh database renders them as `<name>`. Writing the DDL twice
# is exactly how a migrated database and a newly created one come to disagree.
TABLE_BODIES = {
    "patients": """(
    -- NOT NULL is not redundant beside PRIMARY KEY: SQLite permits a NULL in a
    -- TEXT PRIMARY KEY (a documented quirk from before it enforced it), so
    -- without this an INSERT that simply forgot the surrogate would succeed and
    -- create a patient with no identity.
    patient_id TEXT PRIMARY KEY NOT NULL,
    codice_fiscale TEXT UNIQUE NOT NULL,
    patient_name TEXT NOT NULL,
    phone TEXT
)""",
    "visits": """(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    visit_date TEXT,
    procedures TEXT,
    clinical_notes TEXT,
    next_appointment TEXT,
    source_path TEXT UNIQUE NOT NULL
)""",
    "invoices": """(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    visit_id INTEGER NOT NULL REFERENCES visits(id),
    line_index INTEGER NOT NULL,
    amount REAL NOT NULL,
    description TEXT,
    UNIQUE(visit_id, line_index)
)""",
    "appointments": """(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    dentist TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    minutes INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'booked',
    note TEXT,
    period TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)""",
    "patient_credentials": """(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL UNIQUE REFERENCES patients(patient_id),
    pin_hash TEXT NOT NULL,
    must_change_pin INTEGER NOT NULL DEFAULT 1,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    active INTEGER NOT NULL DEFAULT 1
)""",
    "patient_sessions": """(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT UNIQUE NOT NULL,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
)""",
    # The alias tables carry NO foreign key on a codice fiscale. target_cf used
    # to reference patients(codice_fiscale), which kept the CF a relationship
    # key and made correcting a mistyped one raise a foreign-key error - the
    # exact thing this phase exists to remove. source_cf stays as the
    # historical record of what the folded record was called.
    "patient_merges": """(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_cf TEXT NOT NULL UNIQUE,
    target_cf TEXT NOT NULL,
    target_patient_id TEXT REFERENCES patients(patient_id),
    merged_at TEXT NOT NULL,
    merged_by TEXT NOT NULL,
    source_row TEXT NOT NULL,
    moved TEXT NOT NULL
)""",
    "patient_duplicate_dismissals": """(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id_a TEXT NOT NULL,
    patient_id_b TEXT NOT NULL,
    cf_a TEXT NOT NULL,
    cf_b TEXT NOT NULL,
    dismissed_at TEXT NOT NULL,
    dismissed_by TEXT NOT NULL,
    reason TEXT,
    UNIQUE(patient_id_a, patient_id_b)
)""",
}

REBUILT = ("patients", "visits", "invoices", "appointments",
           "patient_credentials", "patient_sessions",
           "patient_merges", "patient_duplicate_dismissals")

SCHEMA_V2 = "\n".join(
    f"CREATE TABLE {name}_v2 {TABLE_BODIES[name]};"
    for name in ("patients", "visits", "invoices", "appointments",
                 "patient_credentials", "patient_sessions"))

ALIAS_SCHEMA_V2 = "\n".join(
    f"CREATE TABLE {name}_v2 {TABLE_BODIES[name]};"
    for name in ("patient_merges", "patient_duplicate_dismissals"))


def fresh_schema():
    """The v2 shape for a database that has never held anything."""
    return "\n".join(
        f"CREATE TABLE IF NOT EXISTS {name} {body};" for name, body in TABLE_BODIES.items())


# (v1 table, v2 table, columns after patient_id)
CHILD_TABLES = [
    ("visits", "visits_v2",
     "visit_date, procedures, clinical_notes, next_appointment, source_path"),
    ("invoices", "invoices_v2", "visit_id, line_index, amount, description"),
    ("appointments", "appointments_v2",
     "dentist, starts_at, minutes, status, note, period, created_at, updated_at"),
    ("patient_credentials", "patient_credentials_v2",
     "pin_hash, must_change_pin, issued_at, expires_at, failed_attempts, locked_until, active"),
    ("patient_sessions", "patient_sessions_v2", "token_hash, created_at, last_seen_at"),
]

INDEXES_V2 = [
    "CREATE INDEX IF NOT EXISTS idx_visits_patient ON visits (patient_id)",
    "CREATE INDEX IF NOT EXISTS idx_invoices_patient ON invoices (patient_id)",
    "CREATE INDEX IF NOT EXISTS idx_appointments_patient ON appointments (patient_id)",
    "CREATE INDEX IF NOT EXISTS idx_appointments_day ON appointments (starts_at)",
    # P05's race stopper, rebuilt on the new table
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_appointments_slot"
    " ON appointments (dentist, starts_at) WHERE status = 'booked'",
]


def _now():
    return datetime.now().isoformat()


def has_pid(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(patients)")}
    return "patient_id" in cols


def ensure_ops_table(conn):
    conn.executescript(OPS_SCHEMA)
    conn.commit()


def _enqueue(conn, step, subject, payload=None):
    conn.execute(
        "INSERT OR IGNORE INTO migration_ops"
        " (migration, step, subject, state, payload, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (MIGRATION, step, subject, PENDING, payload, _now()))


def _mark(conn, step, subject, state, detail=None):
    conn.execute(
        "UPDATE migration_ops SET state = ?, detail = ?, attempts = attempts + 1,"
        " updated_at = ? WHERE migration = ? AND step = ? AND subject = ?",
        (state, detail, _now(), MIGRATION, step, subject))
    conn.commit()


def pending_ops(conn, step=None):
    sql = "SELECT step, subject, payload, detail, attempts FROM migration_ops" \
          " WHERE migration = ? AND state = ?"
    params = [MIGRATION, PENDING]
    if step:
        sql += " AND step = ?"
        params.append(step)
    try:
        return [dict(r) for r in conn.execute(sql + " ORDER BY id", params)]
    except sqlite3.OperationalError:
        return []          # the table does not exist yet: nothing is pending


def orphans(conn):
    """Child rows whose codice fiscale names no patient.

    Checked BEFORE anything moves. The migration copies child rows by joining
    to their patient, and an INNER JOIN would silently DROP an orphan - losing
    a visit or an invoice with no error at all. Foreign keys make orphans
    unlikely, not impossible: PRAGMA foreign_keys is per-connection and any
    process that ever wrote with it off could have left one.
    """
    found = {}
    for table, _, _ in CHILD_TABLES:
        try:
            rows = conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} c"
                " LEFT JOIN patients p ON p.codice_fiscale = c.codice_fiscale"
                " WHERE p.codice_fiscale IS NULL").fetchone()
        except sqlite3.OperationalError:
            continue
        if rows and rows["n"]:
            found[table] = rows["n"]
    return found


def preflight(conn, sorted_root=Path("sorted"), collection=None):
    """What the migration WOULD do. Mutates nothing.

    Deliberately a separate entry point rather than a flag deep inside the
    migration: a dry run that shares a code path with the real thing is one
    typo away from being the real thing.
    """
    report = {"migration": MIGRATION, "already_migrated": has_pid(conn), "blockers": []}
    if report["already_migrated"]:
        report["blockers"].append("this database already carries patient_id")
        return report

    report["patients"] = conn.execute("SELECT COUNT(*) c FROM patients").fetchone()["c"]
    counts = {}
    for table, _, _ in CHILD_TABLES:
        try:
            counts[table] = conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
        except sqlite3.OperationalError:
            counts[table] = 0
    report["rows"] = counts

    found = orphans(conn)
    report["orphans"] = found
    if found:
        report["blockers"].append(
            f"orphan child rows would be dropped by the migration: {found}. "
            "Resolve them before migrating - nothing here deletes patient data.")

    dirs = []
    if Path(sorted_root).is_dir():
        known = {r["codice_fiscale"] for r in conn.execute("SELECT codice_fiscale FROM patients")}
        for child in sorted(Path(sorted_root).iterdir()):
            if child.is_dir() and child.name in known:
                dirs.append(child.name)
    report["sorted_dirs_to_move"] = dirs
    report["bytes_to_move"] = sum(
        f.stat().st_size for d in dirs for f in (Path(sorted_root) / d).rglob("*") if f.is_file())

    if collection is not None:
        try:
            report["chunks"] = collection.count()
        except Exception as e:
            report["chunks"] = f"unavailable: {e}"
    return report


def sqlite_step(conn):
    """Rebuild the six tables onto patient_id. One transaction, or nothing.

    SQLite cannot ALTER a primary key or add a foreign key, so each table is
    recreated and copied. PRAGMA foreign_keys must be set OUTSIDE a transaction
    to take effect, which is why it is toggled around the BEGIN rather than
    inside it.
    """
    if has_pid(conn):
        return {"skipped": "already migrated"}

    found = orphans(conn)
    if found:
        raise RuntimeError(
            f"refusing to migrate: orphan child rows would be dropped ({found}). "
            "Nothing has been changed.")

    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    moved = {}
    try:
        conn.execute("BEGIN")
        conn.executescript(SCHEMA_V2)

        rows = conn.execute(
            "SELECT codice_fiscale, patient_name, phone FROM patients").fetchall()
        assigned = {}
        for row in rows:
            pid = patient_id.new_id()
            assigned[row["codice_fiscale"]] = pid
            conn.execute(
                "INSERT INTO patients_v2 (patient_id, codice_fiscale, patient_name, phone)"
                " VALUES (?, ?, ?, ?)",
                (pid, row["codice_fiscale"], row["patient_name"], row["phone"]))
        moved["patients"] = len(rows)

        for table, new_table, columns in CHILD_TABLES:
            try:
                conn.execute(f"SELECT 1 FROM {table} LIMIT 1")
            except sqlite3.OperationalError:
                continue
            has_id = "id" in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            id_col = "id, " if has_id else ""
            cur = conn.execute(
                f"INSERT INTO {new_table} ({id_col}patient_id, {columns})"
                f" SELECT {('c.id, ' if has_id else '')}p.patient_id,"
                f" {', '.join('c.' + c.strip() for c in columns.split(','))}"
                f" FROM {table} c JOIN patients_v2 p"
                f" ON p.codice_fiscale = c.codice_fiscale")
            moved[table] = cur.rowcount

        # the alias tables, rebuilt so the codice fiscale stops being a
        # relationship key there too - see ALIAS_SCHEMA_V2
        conn.executescript(ALIAS_SCHEMA_V2)
        conn.execute(
            "INSERT INTO patient_merges_v2 (id, source_cf, target_cf, target_patient_id,"
            " merged_at, merged_by, source_row, moved)"
            " SELECT m.id, m.source_cf, m.target_cf, p.patient_id, m.merged_at, m.merged_by,"
            " m.source_row, m.moved FROM patient_merges m"
            " LEFT JOIN patients_v2 p ON p.codice_fiscale = m.target_cf")
        conn.execute(
            "INSERT INTO patient_duplicate_dismissals_v2 (id, patient_id_a, patient_id_b,"
            " cf_a, cf_b, dismissed_at, dismissed_by, reason)"
            " SELECT d.id, COALESCE(a.patient_id, d.cf_a), COALESCE(b.patient_id, d.cf_b),"
            " d.cf_a, d.cf_b, d.dismissed_at, d.dismissed_by, d.reason"
            " FROM patient_duplicate_dismissals d"
            " LEFT JOIN patients_v2 a ON a.codice_fiscale = d.cf_a"
            " LEFT JOIN patients_v2 b ON b.codice_fiscale = d.cf_b")
        for old_name, new_name in (("patient_merges", "patient_merges_v2"),
                                   ("patient_duplicate_dismissals",
                                    "patient_duplicate_dismissals_v2")):
            conn.execute(f"DROP TABLE {old_name}")
            conn.execute(f"ALTER TABLE {new_name} RENAME TO {old_name}")

        for table, new_table, _ in [("patients", "patients_v2", "")] + CHILD_TABLES:
            try:
                conn.execute(f"DROP TABLE {table}")
            except sqlite3.OperationalError:
                pass
            conn.execute(f"ALTER TABLE {new_table} RENAME TO {table}")

        for statement in INDEXES_V2:
            conn.execute(statement)

        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise RuntimeError(f"foreign keys broken after rebuild: {broken[:5]}")

        # enqueue the other two stores IN THIS TRANSACTION. if the process dies
        # after the commit, the work that still has to happen is already
        # recorded - that is what makes the state machine recoverable rather
        # than a hope.
        conn.executescript(OPS_SCHEMA)
        for cf, pid in assigned.items():
            _enqueue(conn, STEP_CHROMA, pid, cf)
            _enqueue(conn, STEP_FS, pid, cf)
        conn.commit()
    except Exception:
        conn.rollback()
        for new_table in (["patients_v2", "patient_merges_v2",
                           "patient_duplicate_dismissals_v2"]
                          + [t[1] for t in CHILD_TABLES]):
            try:
                conn.execute(f"DROP TABLE IF EXISTS {new_table}")
            except sqlite3.OperationalError:
                pass
        conn.commit()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    return moved


def chroma_step(conn, collection):
    """Give every chunk its patient_id and the CURRENT patient name.

    The name matters as much as the id: `agent.update_field` writes SQLite only,
    so a renamed patient kept the old name in chunk metadata and staff Q&A cited
    a name the record no longer had. Migrating the metadata is the natural place
    to fix it, because the metadata is being rewritten anyway.
    """
    done = []
    for op in pending_ops(conn, STEP_CHROMA):
        pid, cf = op["subject"], op["payload"]
        row = conn.execute(
            "SELECT patient_name FROM patients WHERE patient_id = ?", (pid,)).fetchone()
        if row is None:
            _mark(conn, STEP_CHROMA, pid, DONE, "patient no longer present")
            continue
        try:
            found = collection.get(where={"codice_fiscale": cf})
            ids = found.get("ids") or []
            if ids:
                metadatas = []
                for meta in found.get("metadatas") or []:
                    fresh = dict(meta)
                    fresh["patient_id"] = pid
                    fresh["patient_name"] = row["patient_name"]
                    metadatas.append(fresh)
                collection.update(ids=ids, metadatas=metadatas)
            _mark(conn, STEP_CHROMA, pid, DONE, f"{len(ids)} chunk(s)")
            done.append({"patient_id": pid, "chunks": len(ids)})
        except Exception as e:
            # FAILED, not silently skipped: an index nobody knows is stale is
            # worse than one that says so.
            _mark(conn, STEP_CHROMA, pid, FAILED, str(e)[:200])
    return done


def filesystem_step(conn, sorted_root=Path("sorted")):
    """Move `sorted/<CF>/` to `sorted/<patient_id>/` and repoint source_path.

    The directory rename and the `visits.source_path` rewrite are committed
    together, so a path row and its file cannot disagree for longer than one
    statement. If the move fails the op stays pending and the original
    directory is untouched - nothing is deleted here.
    """
    sorted_root = Path(sorted_root)
    done = []
    for op in pending_ops(conn, STEP_FS):
        pid, cf = op["subject"], op["payload"]
        src, dest = sorted_root / cf, sorted_root / pid
        try:
            if dest.exists() and not src.exists():
                _mark(conn, STEP_FS, pid, DONE, "already moved")
                continue
            if not cf:
                # the payload is what says WHICH directory to move. without it
                # the op cannot be completed, and calling it done would be the
                # silent-inconsistency failure this ledger exists to prevent.
                _mark(conn, STEP_FS, pid, FAILED,
                      "no payload recorded - cannot tell which directory to move")
                continue
            if not src.exists():
                _mark(conn, STEP_FS, pid, DONE, "no directory for this patient")
                continue
            if dest.exists():
                # both present: a previous run moved some files and stopped.
                # merge rather than overwrite, and never clobber.
                for item in src.rglob("*"):
                    if item.is_file():
                        target = dest / item.relative_to(src)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if not target.exists():
                            shutil.move(str(item), str(target))
                shutil.rmtree(src, ignore_errors=True)
            else:
                shutil.move(str(src), str(dest))

            conn.execute(
                "UPDATE visits SET source_path = replace(source_path, ?, ?)"
                " WHERE patient_id = ? AND source_path LIKE ?",
                (f"/{cf}/", f"/{pid}/", pid, f"%/{cf}/%"))
            _mark(conn, STEP_FS, pid, DONE, f"{cf} -> {pid}")
            done.append({"patient_id": pid, "from": cf})
        except Exception as e:
            _mark(conn, STEP_FS, pid, FAILED, str(e)[:200])
    return done


def run(conn, sorted_root=Path("sorted"), collection=None):
    """The whole migration, resumable. Safe to run repeatedly."""
    result = {"sqlite": None, "chroma": [], "filesystem": []}
    ensure_ops_table(conn)
    if not has_pid(conn):
        result["sqlite"] = sqlite_step(conn)
    else:
        result["sqlite"] = {"skipped": "already migrated"}
    if collection is not None:
        result["chroma"] = chroma_step(conn, collection)
    result["filesystem"] = filesystem_step(conn, sorted_root)
    result["still_pending"] = pending_ops(conn)
    return result


def status(conn):
    try:
        rows = conn.execute(
            "SELECT step, state, COUNT(*) AS n FROM migration_ops"
            " WHERE migration = ? GROUP BY step, state", (MIGRATION,)).fetchall()
    except sqlite3.OperationalError:
        return {"migrated": has_pid(conn), "ops": {}}
    return {"migrated": has_pid(conn),
            "ops": {f"{r['step']}/{r['state']}": r["n"] for r in rows}}


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        import migrate_pid_selftest
        migrate_pid_selftest.selftest()
        return
    print("usage: python migrate_pid.py --selftest")


if __name__ == "__main__":
    main()
