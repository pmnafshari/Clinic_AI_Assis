"""Take codici fiscali and patient names out of the audit trail already written.

log_audit stops new rows carrying them (P06.04). This is the one-off for the
rows written before that: an identity field that is a codice fiscale becomes the
surrogate id, one that is a patient's name becomes the same, and log.txt has its
codici fiscali and absolute paths redacted. An encrypted backup is taken first,
the rewrite is one transaction, and running it again changes nothing.

    python migrate_audit.py            report only
    python migrate_audit.py --apply    back up, then rewrite
"""
import json
import sys
from pathlib import Path

import action_log
import auth
import backup
import storage

DB_PATH = "db/clinic.sqlite"
LOG_PATH = "log.txt"


def plan(conn):
    names = {}
    for row in conn.execute("SELECT patient_id, patient_name FROM patients"):
        names[row["patient_name"].strip().casefold()] = row["patient_id"]

    changes = []
    for row in conn.execute("SELECT id, username, role, target FROM audit_log"):
        username = auth._pseudonymise(conn, row["username"])
        target = auth._pseudonymise(conn, row["target"])
        if target and target.strip().casefold() in names:
            target = names[target.strip().casefold()]
        if row["role"] == "patient" and username.strip().casefold() in names:
            username = names[username.strip().casefold()]
        if (username, target) != (row["username"], row["target"]):
            changes.append((row["id"], username, target))
    return changes


def rewrite_log(log_path):
    path = Path(log_path)
    if not path.exists():
        return 0
    changed = 0
    lines = []
    for line in path.read_text().splitlines():
        parts = line.split(" | ")
        if len(parts) == 4:
            cleaned = " | ".join([parts[0], action_log._clean(parts[1]), action_log._clean(parts[2]),
                                  action_log.CF_SHAPE.sub("<cf>", parts[3])])
        else:
            cleaned = action_log.CF_SHAPE.sub("<cf>", line)
        changed += cleaned != line
        lines.append(cleaned)
    path.write_text("\n".join(lines) + ("\n" if lines else ""))
    return changed


def apply(conn, changes):
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("INSERT INTO audit_unlock (reason) VALUES ('migrate_audit')")
        for row_id, username, target in changes:
            conn.execute("UPDATE audit_log SET username = ?, target = ? WHERE id = ?",
                         (username, target, row_id))
        conn.execute("DELETE FROM audit_unlock")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    auth.log_audit(conn, "migrate_audit", "system", "audit_migrate", f"{len(changes)} rows",
                   allowed=1, reason="pseudonymise identity fields")


def run(db_path=DB_PATH, log_path=LOG_PATH, do_apply=False, data_root=".", key_path=None):
    conn = storage.init_db(db_path)
    try:
        changes = plan(conn)
        report = {"audit_rows": len(changes), "applied": False}
        if not do_apply:
            return report
        if changes:
            report["backup"] = backup.create(data_root=data_root, key_path=key_path)["archive"]
            apply(conn, changes)
        report["log_lines"] = rewrite_log(log_path)
        report["applied"] = True
        report["left"] = len(plan(conn))
        return report
    finally:
        conn.close()


def selftest():
    import tempfile

    import patient_id

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "db").mkdir()
        key = root / "k.key"
        backup.init_key(key)
        db_path = root / "db" / "clinic.sqlite"
        conn = storage.init_db(str(db_path))
        pid = patient_id.seed_patient(conn, "ZZMA800101010101", "Mara Audit")
        # rows as the old code wrote them, before log_audit pseudonymised
        conn.execute("INSERT INTO audit_unlock (reason) VALUES ('seed')")
        for username, role, target in [
                ("ZZMA800101010101", "patient", "ZZMA800101010101"),
                ("drossi", "assistant", "mara audit"),
                ("drossi", "dentist", "sorted/x/notes/a.json"),
                ("drossi", "dentist", "ZZMA800101010199")]:
            conn.execute("INSERT INTO audit_log (ts, username, role, action, target, allowed)"
                         " VALUES ('2026-07-01T10:00:00+00:00', ?, ?, 'read_notes', ?, 1)",
                         (username, role, target))
        conn.execute("DELETE FROM audit_unlock")
        conn.commit()
        conn.close()
        log = root / "log.txt"
        log.write_text("2026-07-06 12:25:40 | /var/folders/T/tmp/drop/fattura_ZZMA800101010101.xlsx"
                       " | /var/folders/T/tmp/sorted/ZZMA800101010101/records/f.xlsx"
                       " | type:xlsx cf:ZZMA800101010101\n")

        # 1. report only changes nothing
        report = run(str(db_path), str(log), data_root=root, key_path=key)
        assert report == {"audit_rows": 3, "applied": False}, f"1: {report}"
        assert "ZZMA800101010101" in log.read_text(), "1: a report must not touch the log"

        # 2. apply backs up, rewrites, and leaves nothing to do
        report = run(str(db_path), str(log), do_apply=True, data_root=root, key_path=key)
        assert report["applied"] and report["left"] == 0 and report["log_lines"] == 1, f"2: {report}"
        assert Path(report["backup"]).exists(), "2: no backup was taken"
        conn = storage.connect(str(db_path))
        rows = conn.execute("SELECT username, target FROM audit_log WHERE action = 'read_notes'"
                            " ORDER BY id").fetchall()
        assert [tuple(r) for r in rows] == [
            (pid, pid), ("drossi", pid), ("drossi", "sorted/x/notes/a.json"),
            ("drossi", "cf-unknown")], f"2: {[tuple(r) for r in rows]}"
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'audit_migrate'"
                            ).fetchone()[0] == 1, "2: the migration must audit itself"
        conn.close()
        text = log.read_text()
        assert "ZZMA" not in text and "/var/folders" not in text, f"2: log not clean: {text}"

        # 3. a second run is a no-op
        report = run(str(db_path), str(log), do_apply=True, data_root=root, key_path=key)
        assert report["audit_rows"] == 0 and report["log_lines"] == 0, f"3: {report}"
        assert "backup" not in report, "3: nothing to change needs no backup"

    print("selftest ok")


def main(argv):
    if "--selftest" in argv:
        selftest()
        return
    print(json.dumps(run(do_apply="--apply" in argv), indent=2))


if __name__ == "__main__":
    main(sys.argv[1:])
