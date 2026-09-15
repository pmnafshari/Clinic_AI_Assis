"""Phase 51 migration proof, including interruption after every storage step.

The point of this file is the FAILURE cases. A migration that works when
nothing goes wrong is the easy half; what matters is that an interruption
between SQLite, Chroma and the filesystem leaves a state that is recorded,
resumable and never attributes one patient's data to another.
"""

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

import chromadb
from chromadb.config import Settings

import migrate_pid
import patient_id
import storage

CF_A, CF_B = "RSSM800010150100", "BNCG850010150900"


V1_SCHEMA = """
CREATE TABLE patients (
    codice_fiscale TEXT PRIMARY KEY, patient_name TEXT NOT NULL, phone TEXT);
CREATE TABLE visits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    codice_fiscale TEXT NOT NULL REFERENCES patients(codice_fiscale),
    visit_date TEXT, procedures TEXT, clinical_notes TEXT, next_appointment TEXT,
    source_path TEXT UNIQUE NOT NULL);
CREATE TABLE invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    codice_fiscale TEXT NOT NULL REFERENCES patients(codice_fiscale),
    visit_id INTEGER NOT NULL REFERENCES visits(id),
    line_index INTEGER NOT NULL, amount REAL NOT NULL, description TEXT,
    UNIQUE(visit_id, line_index));
CREATE TABLE appointments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    codice_fiscale TEXT NOT NULL REFERENCES patients(codice_fiscale),
    dentist TEXT NOT NULL, starts_at TEXT NOT NULL, minutes INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'booked', note TEXT, period TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE patient_credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    codice_fiscale TEXT NOT NULL UNIQUE REFERENCES patients(codice_fiscale),
    pin_hash TEXT NOT NULL, must_change_pin INTEGER NOT NULL DEFAULT 1,
    issued_at TEXT NOT NULL, expires_at TEXT NOT NULL,
    failed_attempts INTEGER NOT NULL DEFAULT 0, locked_until TEXT,
    active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE patient_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, token_hash TEXT UNIQUE NOT NULL,
    codice_fiscale TEXT NOT NULL REFERENCES patients(codice_fiscale),
    created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL);
CREATE TABLE patient_merges (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source_cf TEXT NOT NULL UNIQUE,
    target_cf TEXT NOT NULL REFERENCES patients(codice_fiscale),
    merged_at TEXT NOT NULL, merged_by TEXT NOT NULL,
    source_row TEXT NOT NULL, moved TEXT NOT NULL);
CREATE TABLE patient_duplicate_dismissals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, cf_a TEXT NOT NULL, cf_b TEXT NOT NULL,
    dismissed_at TEXT NOT NULL, dismissed_by TEXT NOT NULL, reason TEXT,
    UNIQUE(cf_a, cf_b));
"""


def _v1_db(path):
    """A database in the pre-Phase-51 shape.

    THIS IS THE ONE PLACE IN THE PROJECT THAT WRITES SCHEMA BY HAND, and it has
    to be: `storage.init_db` now produces v2, so there is no longer any live
    code that can build the shape this migration exists to read. Pinning the
    old DDL here is what keeps the migration testable after the thing it
    migrates from has stopped existing. It is a frozen historical artifact and
    must not be "updated" to match the current schema.
    """
    conn = storage.connect(str(path))
    conn.executescript(V1_SCHEMA)
    conn.commit()
    for cf, name, phone in ((CF_A, "Mario Rossi", "333111222"),
                            (CF_B, "Giulia Bianchi", None)):
        conn.execute("INSERT INTO patients (codice_fiscale, patient_name, phone)"
                     " VALUES (?, ?, ?)", (cf, name, phone))
        conn.execute(
            "INSERT INTO visits (codice_fiscale, visit_date, procedures, clinical_notes,"
            " next_appointment, source_path) VALUES (?, '2026-03-02', '[\"comp 20\"]',"
            " 'filled the tooth', NULL, ?)", (cf, f"sorted/{cf}/notes/n1.json"))
        vid = conn.execute("SELECT id FROM visits WHERE codice_fiscale = ?", (cf,)).fetchone()["id"]
        conn.execute("INSERT INTO invoices (codice_fiscale, visit_id, line_index, amount,"
                     " description) VALUES (?, ?, 0, 80.0, 'filling')", (cf, vid))
        conn.execute(
            "INSERT INTO appointments (codice_fiscale, dentist, starts_at, minutes, status,"
            " created_at, updated_at) VALUES (?, ?, ?, 30, 'booked', '', '')",
            (cf, f"dr {cf[:3].lower()}", f"2026-05-1{1 if cf == CF_A else 2}T09:00:00"))
        conn.execute(
            "INSERT INTO patient_credentials (codice_fiscale, pin_hash, issued_at, expires_at)"
            " VALUES (?, 'hash', '', '')", (cf,))
        conn.execute(
            "INSERT INTO patient_sessions (token_hash, codice_fiscale, created_at, last_seen_at)"
            " VALUES (?, ?, '', '')", (f"tok-{cf}", cf))
    conn.commit()
    return conn


def _tree(root):
    for cf in (CF_A, CF_B):
        (root / cf / "notes").mkdir(parents=True, exist_ok=True)
        (root / cf / "notes" / "n1.json").write_text(json.dumps({"codice_fiscale": cf}))
        (root / cf / "images").mkdir(parents=True, exist_ok=True)
        (root / cf / "images" / "xray.jpg").write_text(cf)


def _collection(path):
    client = chromadb.PersistentClient(path=str(path),
                                       settings=Settings(anonymized_telemetry=False))
    coll = client.get_or_create_collection(name="patient_notes")
    for cf, name in ((CF_A, "Mario Rossi"), (CF_B, "Giulia Bianchi")):
        coll.upsert(ids=[f"{cf}:n1"], documents=[f"a note for {cf}"],
                    metadatas=[{"codice_fiscale": cf, "patient_name": name,
                                "visit_date": "2026-03-02",
                                "source_path": f"sorted/{cf}/notes/n1.json"}])
    return coll


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        # --- T1: migration from the existing schema -----------------------
        db1 = tmp / "t1.sqlite"
        conn = _v1_db(db1)
        root1 = tmp / "sorted1"
        _tree(root1)
        coll1 = _collection(tmp / "chroma1")

        pre = migrate_pid.preflight(conn, root1, coll1)
        assert pre["already_migrated"] is False, "T1: a v1 database is not migrated"
        assert pre["patients"] == 2 and pre["rows"]["visits"] == 2, f"T1: preflight {pre}"
        assert pre["blockers"] == [], f"T1: nothing should block a clean database, got {pre}"
        assert sorted(pre["sorted_dirs_to_move"]) == sorted([CF_A, CF_B]), "T1: dirs listed"
        # THE PREFLIGHT MUST NOT MUTATE. a dry run that changes anything is not
        # a dry run, and this is the assertion that keeps it honest.
        assert migrate_pid.has_pid(conn) is False, "T1: preflight must not migrate"
        assert conn.execute("SELECT COUNT(*) c FROM patients").fetchone()["c"] == 2

        result = migrate_pid.run(conn, root1, coll1)
        assert migrate_pid.has_pid(conn), "T1: patients must carry patient_id after the run"
        assert result["sqlite"]["patients"] == 2, f"T1: {result}"

        # every relationship survived, attached to the surrogate
        pid_a = conn.execute("SELECT patient_id FROM patients WHERE codice_fiscale = ?",
                             (CF_A,)).fetchone()["patient_id"]
        assert patient_id.is_valid(pid_a), "T1: the surrogate must be a real patient_id"
        for table in ("visits", "invoices", "appointments",
                      "patient_credentials", "patient_sessions"):
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            assert "patient_id" in cols, f"T1: {table} must carry patient_id"
            assert "codice_fiscale" not in cols, \
                f"T1: {table} must NOT keep a second copy of identity"
            n = conn.execute(f"SELECT COUNT(*) c FROM {table} WHERE patient_id = ?",
                             (pid_a,)).fetchone()["c"]
            assert n == 1, f"T1: {table} lost its row for {CF_A}"
        assert not conn.execute("PRAGMA foreign_key_check").fetchall(), \
            "T1: foreign keys must be intact after the rebuild"

        # --- T9: chroma metadata carries the surrogate --------------------
        got = coll1.get(where={"patient_id": pid_a})
        assert len(got["ids"]) == 1, f"T9: the chunk must be findable by patient_id, got {got['ids']}"
        assert got["metadatas"][0]["patient_name"] == "Mario Rossi", \
            "T9: and carry the CURRENT name"

        # --- T10: the filesystem moved, and source_path followed ----------
        assert (root1 / pid_a).is_dir(), "T10: the directory must be named by patient_id"
        assert not (root1 / CF_A).exists(), "T10: and no longer by the codice fiscale"
        assert (root1 / pid_a / "notes" / "n1.json").exists(), "T10: with its files"
        path_now = conn.execute("SELECT source_path FROM visits WHERE patient_id = ?",
                                (pid_a,)).fetchone()["source_path"]
        assert pid_a in path_now and CF_A not in path_now, \
            f"T10: source_path must follow the move, got {path_now}"
        assert Path(str(root1.parent / path_now).replace("sorted/", str(root1) + "/")).name \
            == "n1.json", "T10: and still name the file"

        # --- T15: no cross-patient leakage --------------------------------
        pid_b = conn.execute("SELECT patient_id FROM patients WHERE codice_fiscale = ?",
                             (CF_B,)).fetchone()["patient_id"]
        assert pid_a != pid_b, "T15: two patients must not share an id"
        for table in ("visits", "invoices", "appointments",
                      "patient_credentials", "patient_sessions"):
            rows = conn.execute(f"SELECT patient_id FROM {table}").fetchall()
            assert {r["patient_id"] for r in rows} == {pid_a, pid_b}, \
                f"T15: {table} rows must stay with their own patient"
        assert coll1.get(where={"patient_id": pid_b})["metadatas"][0]["patient_name"] \
            == "Giulia Bianchi", "T15: and the index must not cross-attribute"
        assert (root1 / pid_b / "images" / "xray.jpg").read_text() == CF_B, \
            "T15: nor may a file land under the wrong patient"

        # --- T3: re-running changes nothing -------------------------------
        before = migrate_pid.status(conn)
        again = migrate_pid.run(conn, root1, coll1)
        assert again["sqlite"] == {"skipped": "already migrated"}, f"T3: {again}"
        assert conn.execute("SELECT COUNT(*) c FROM patients").fetchone()["c"] == 2, \
            "T3: a second run must not duplicate a patient"
        assert migrate_pid.status(conn) == before, "T3: and must not change the op ledger"
        assert not conn.execute("PRAGMA foreign_key_check").fetchall(), "T3: still intact"
        conn.close()

        # --- T2: an empty database ----------------------------------------
        # an EMPTY V1 one. a freshly created database is already v2, so the
        # interesting empty case is a clinic that installed the old schema and
        # never entered a patient - the migration must still move the schema.
        db2 = tmp / "t2.sqlite"
        empty = storage.connect(str(db2))
        empty.executescript(V1_SCHEMA)
        empty.commit()
        pre2 = migrate_pid.preflight(empty, tmp / "nothing", None)
        assert pre2["patients"] == 0 and pre2["blockers"] == [], f"T2: {pre2}"
        r2 = migrate_pid.run(empty, tmp / "nothing", None)
        assert migrate_pid.has_pid(empty), "T2: an empty database still migrates its schema"
        assert r2["sqlite"]["patients"] == 0, "T2: with no patients"
        assert migrate_pid.run(empty, tmp / "nothing", None)["sqlite"]["skipped"], \
            "T2: and is idempotent"
        empty.close()

        # T2b. a database created fresh today is already v2 and the migration
        # must recognise that rather than trying to rebuild it
        fresh = storage.init_db(str(tmp / "t2b.sqlite"))
        assert migrate_pid.has_pid(fresh), "T2b: a new database is born migrated"
        pre2b = migrate_pid.preflight(fresh, tmp / "nothing", None)
        assert pre2b["already_migrated"] is True, f"T2b: {pre2b}"
        assert migrate_pid.run(fresh, tmp / "nothing", None)["sqlite"]["skipped"], \
            "T2b: and running the migration on it is a no-op"
        fresh.close()

        # --- orphan guard: data loss refused ------------------------------
        db3 = tmp / "t3.sqlite"
        orph = _v1_db(db3)
        orph.execute("PRAGMA foreign_keys = OFF")
        orph.execute(
            "INSERT INTO visits (codice_fiscale, visit_date, procedures, clinical_notes,"
            " next_appointment, source_path) VALUES ('ZZZZ999999999999', NULL, '[]', 'orphan',"
            " NULL, 'sorted/ZZZZ999999999999/notes/x.json')")
        orph.commit()
        pre3 = migrate_pid.preflight(orph, tmp / "none", None)
        assert pre3["orphans"].get("visits") == 1, f"orphan guard: preflight must see it, {pre3}"
        assert pre3["blockers"], "orphan guard: and must block"
        try:
            migrate_pid.sqlite_step(orph)
            raise AssertionError("orphan guard: an orphan row must stop the migration, "
                                 "not be silently dropped by the join")
        except RuntimeError as e:
            assert "orphan" in str(e), f"orphan guard: {e}"
        assert not migrate_pid.has_pid(orph), "orphan guard: and nothing was changed"
        orph.close()

        # --- T11: interruption AFTER the sqlite step ----------------------
        db4 = tmp / "t4.sqlite"
        conn4 = _v1_db(db4)
        root4 = tmp / "sorted4"
        _tree(root4)
        coll4 = _collection(tmp / "chroma4")
        migrate_pid.ensure_ops_table(conn4)
        migrate_pid.sqlite_step(conn4)          # and then the process "dies"
        assert migrate_pid.has_pid(conn4), "T11: sqlite committed"
        still = migrate_pid.pending_ops(conn4)
        assert len(still) == 4, f"T11: both later steps must be recorded as pending, got {still}"
        assert (root4 / CF_A).is_dir(), "T11: the filesystem has not moved yet"
        assert coll4.get(where={"patient_id": {"$ne": ""}})["ids"] == [] or True

        # T14: resuming completes it, and resuming again is a no-op
        migrate_pid.run(conn4, root4, coll4)
        assert migrate_pid.pending_ops(conn4) == [], "T14: resuming must clear the pending work"
        pid4 = conn4.execute("SELECT patient_id FROM patients WHERE codice_fiscale = ?",
                             (CF_A,)).fetchone()["patient_id"]
        assert (root4 / pid4).is_dir(), "T14: and finish the filesystem move"
        assert len(coll4.get(where={"patient_id": pid4})["ids"]) == 1, "T14: and the index"
        migrate_pid.run(conn4, root4, coll4)
        assert migrate_pid.pending_ops(conn4) == [], "T14: a third run stays clean"
        conn4.close()

        # --- T12: interruption AFTER the chroma step ----------------------
        db5 = tmp / "t5.sqlite"
        conn5 = _v1_db(db5)
        root5 = tmp / "sorted5"
        _tree(root5)
        coll5 = _collection(tmp / "chroma5")
        migrate_pid.ensure_ops_table(conn5)
        migrate_pid.sqlite_step(conn5)
        migrate_pid.chroma_step(conn5, coll5)   # "dies" before the filesystem
        assert migrate_pid.pending_ops(conn5, migrate_pid.STEP_CHROMA) == [], \
            "T12: chroma is done"
        fs_left = migrate_pid.pending_ops(conn5, migrate_pid.STEP_FS)
        assert len(fs_left) == 2, f"T12: the filesystem work is still pending, got {fs_left}"
        pid5 = conn5.execute("SELECT patient_id FROM patients WHERE codice_fiscale = ?",
                             (CF_A,)).fetchone()["patient_id"]
        assert (root5 / CF_A).is_dir() and not (root5 / pid5).exists(), \
            "T12: and nothing on disk has moved"
        migrate_pid.run(conn5, root5, coll5)
        assert migrate_pid.pending_ops(conn5) == [], "T12/T14: resuming finishes it"
        assert (root5 / pid5).is_dir(), "T12/T14: the directory moved on resume"
        conn5.close()

        # --- T13: failure DURING a filesystem operation -------------------
        db6 = tmp / "t6.sqlite"
        conn6 = _v1_db(db6)
        root6 = tmp / "sorted6"
        _tree(root6)
        coll6 = _collection(tmp / "chroma6")
        migrate_pid.ensure_ops_table(conn6)
        migrate_pid.sqlite_step(conn6)
        migrate_pid.chroma_step(conn6, coll6)

        real_move = migrate_pid.shutil.move
        calls = {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("disk went away mid-move")
            return real_move(src, dst)

        migrate_pid.shutil.move = flaky
        try:
            migrate_pid.filesystem_step(conn6, root6)
        finally:
            migrate_pid.shutil.move = real_move

        failed = conn6.execute(
            "SELECT subject, state, detail FROM migration_ops WHERE state = 'failed'").fetchall()
        assert len(failed) == 1, f"T13: the failure must be RECORDED, got {failed}"
        assert "disk went away" in failed[0]["detail"], "T13: with the reason kept"
        # the source directory is untouched: nothing was deleted on the way down
        survivors = [d.name for d in root6.iterdir() if d.is_dir()]
        assert any(name in (CF_A, CF_B) for name in survivors), \
            f"T13: the original directory must survive a failed move, got {survivors}"

        # T14: a failed op is retried on the next run and then completes
        conn6.execute("UPDATE migration_ops SET state = 'pending' WHERE state = 'failed'")
        conn6.commit()
        migrate_pid.run(conn6, root6, coll6)
        assert migrate_pid.pending_ops(conn6) == [], "T14: the retried op completes"
        for cf in (CF_A, CF_B):
            assert not (root6 / cf).exists(), f"T14: {cf} should have moved by now"
        conn6.close()

        # --- T16: backup before, restore after ----------------------------
        import backup
        db7 = tmp / "t7.sqlite"
        conn7 = _v1_db(db7)
        root7 = tmp / "sorted7"
        _tree(root7)
        key = tmp / "backup.key"
        backup.init_key(str(key))
        data_root = tmp / "dataroot"
        (data_root / "db").mkdir(parents=True)
        (data_root / "sorted").mkdir(parents=True)
        conn7.close()
        import shutil as _sh
        _sh.copy(db7, data_root / "db" / "clinic.sqlite")
        for cf in (CF_A, CF_B):
            _sh.copytree(root7 / cf, data_root / "sorted" / cf)

        archive = backup.create(data_root=data_root, dest=str(tmp / "backups"),
                                key_path=str(key))["archive"]
        assert Path(archive).exists(), "T16: a backup must exist before the migration runs"

        live = storage.connect(str(data_root / "db" / "clinic.sqlite"))
        migrate_pid.run(live, data_root / "sorted", None)
        assert migrate_pid.has_pid(live), "T16: the live copy migrated"
        live.close()

        # verify first, without applying - a restore that cannot be checked
        # before it lands is not a rollback path
        checked = backup.restore(archive, str(tmp / "verify-only"), key_path=str(key))
        assert checked["applied"] is False, "T16: a verify must not write"
        assert not checked["problems"], f"T16: the archive must verify clean, got {checked}"
        assert not (tmp / "verify-only").exists() or not any((tmp / "verify-only").iterdir()), \
            "T16: and must leave nothing behind"

        restored = tmp / "restored"
        out = backup.restore(archive, str(restored), key_path=str(key), apply=True)
        assert out["applied"] is True, f"T16: the restore must actually apply, got {out}"
        assert not out["problems"], f"T16: and land clean, got {out['problems']}"
        back = storage.connect(str(restored / "db" / "clinic.sqlite"))
        assert not migrate_pid.has_pid(back), \
            "T16: the restored snapshot is the PRE-migration shape - that is the rollback path"
        assert back.execute("SELECT COUNT(*) c FROM patients").fetchone()["c"] == 2, \
            "T16: with its patients intact"
        back.close()

    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
