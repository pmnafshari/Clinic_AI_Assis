import json
import sqlite3
import sys
import threading
from datetime import date, datetime
from pathlib import Path

import chromadb
from chromadb.config import Settings
# internal chroma path, pinned by chromadb==1.3.7 in requirements.txt
from chromadb.api.client import SharedSystemClient

from auth import authorize, log_audit
from dental_notes_schema import DentalNote


def connect(db_path):
    # the app, the upload worker and the watcher all write this file from
    # separate threads and processes. the default rollback journal takes an
    # EXCLUSIVE lock, so without WAL any overlap is a "database is locked".
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path):
    conn = connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS patients (
            codice_fiscale TEXT PRIMARY KEY,
            patient_name TEXT NOT NULL,
            phone TEXT
        );
        CREATE TABLE IF NOT EXISTS visits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            codice_fiscale TEXT NOT NULL REFERENCES patients(codice_fiscale),
            visit_date TEXT,
            procedures TEXT,
            clinical_notes TEXT,
            next_appointment TEXT,
            source_path TEXT UNIQUE NOT NULL
        );
        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            codice_fiscale TEXT NOT NULL REFERENCES patients(codice_fiscale),
            visit_id INTEGER NOT NULL REFERENCES visits(id),
            line_index INTEGER NOT NULL,
            amount REAL NOT NULL,
            description TEXT,
            UNIQUE(visit_id, line_index)
        );
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('dentist', 'assistant', 'admin')),
            active INTEGER NOT NULL DEFAULT 1,
            must_change_password INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            username TEXT NOT NULL,
            role TEXT NOT NULL,
            action TEXT NOT NULL,
            target TEXT,
            allowed INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_hash TEXT UNIQUE NOT NULL,
            username TEXT NOT NULL,
            role TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pending_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_hash TEXT UNIQUE NOT NULL,
            username TEXT NOT NULL,
            role TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
    """)
    _ensure_lockout_columns(conn)
    conn.commit()
    return conn


def _ensure_lockout_columns(conn):
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "failed_attempts" not in existing:
        conn.execute("ALTER TABLE users ADD COLUMN failed_attempts INTEGER NOT NULL DEFAULT 0")
    if "locked_until" not in existing:
        conn.execute("ALTER TABLE users ADD COLUMN locked_until TEXT")
    if "must_change_password" not in existing:
        conn.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")


def upsert_note_sql(note, source_path, conn):
    visit_date = note.visit_date.isoformat() if note.visit_date else None

    conn.execute("""
        INSERT INTO patients (codice_fiscale, patient_name, phone)
        VALUES (?, ?, ?)
        ON CONFLICT(codice_fiscale) DO UPDATE SET
            patient_name = excluded.patient_name,
            phone = excluded.phone
    """, (note.codice_fiscale, note.patient_name, note.phone))

    conn.execute("""
        INSERT INTO visits
            (codice_fiscale, visit_date, procedures, clinical_notes, next_appointment, source_path)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_path) DO UPDATE SET
            visit_date = excluded.visit_date,
            procedures = excluded.procedures,
            clinical_notes = excluded.clinical_notes,
            next_appointment = excluded.next_appointment
    """, (note.codice_fiscale, visit_date, json.dumps(note.procedures),
          note.clinical_notes, note.next_appointment, source_path))

    visit_id = conn.execute(
        "SELECT id FROM visits WHERE source_path = ?", (source_path,)
    ).fetchone()["id"]

    # invoices are positional; wipe and re-insert so the set stays authoritative
    # when a re-imported note has fewer lines than before
    conn.execute("DELETE FROM invoices WHERE visit_id = ?", (visit_id,))
    for i, inv in enumerate(note.invoices):
        conn.execute(
            "INSERT INTO invoices (codice_fiscale, visit_id, line_index, amount, description)"
            " VALUES (?, ?, ?, ?, ?)",
            (note.codice_fiscale, visit_id, i, inv.amount, inv.description),
        )

    conn.commit()


def lookup_patient(cf, conn):
    patient = conn.execute(
        "SELECT patient_name, phone FROM patients WHERE codice_fiscale = ?", (cf,)
    ).fetchone()
    if patient is None:
        return None

    visits = conn.execute(
        "SELECT visit_date FROM visits WHERE codice_fiscale = ? ORDER BY id", (cf,)
    ).fetchall()

    invoices = conn.execute(
        "SELECT amount, description FROM invoices WHERE codice_fiscale = ? ORDER BY id", (cf,)
    ).fetchall()

    return {
        "patient_name": patient["patient_name"],
        "phone": patient["phone"],
        "visit_dates": [v["visit_date"] for v in visits],
        "invoices": [{"amount": i["amount"], "description": i["description"]} for i in invoices],
    }


def lookup_clinical(cf, conn):
    # dentist-only visit detail - kept separate from lookup_patient's
    # CRM-only contract so existing callers are unaffected
    visits = conn.execute(
        "SELECT id, visit_date, procedures, clinical_notes, next_appointment"
        " FROM visits WHERE codice_fiscale = ? ORDER BY id", (cf,)
    ).fetchall()

    return [
        {
            "id": v["id"],
            "visit_date": v["visit_date"],
            "procedures": json.loads(v["procedures"]) if v["procedures"] else [],
            "clinical_notes": v["clinical_notes"],
            "next_appointment": v["next_appointment"],
        }
        for v in visits
    ]


SYSTEM_USERNAME = "system"
SYSTEM_ROLE = "system"


def get_collection(chroma_path):
    # first run downloads the ~83MB all-MiniLM-L6-v2 ONNX model once, then it's cached
    client = chromadb.PersistentClient(
        path=chroma_path,
        settings=Settings(anonymized_telemetry=False),
    )
    return client.get_or_create_collection(name="patient_notes")


_shared_collection = None
_shared_collection_path = None
_shared_collection_stamp = None
_shared_refresh_lock = threading.Lock()


def _chroma_stamp(chroma_path):
    # mtime+size of chroma's own sqlite file. a row count misses an in-place
    # re-upsert of an existing chunk - which is exactly what --backfill does
    # to an already-present note - and that is the case that leaves this
    # process querying the pre-repair text (measured, 14-08).
    path = Path(chroma_path) / "chroma.sqlite3"
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _note_own_write(collection):
    # this process just wrote through the shared handle, so its hnsw reader
    # is already current - record the new stamp or the next caller rebuilds
    # the whole client (and its ~83MB embedder) for nothing
    global _shared_collection_stamp
    with _shared_refresh_lock:
        if collection is _shared_collection:
            _shared_collection_stamp = _chroma_stamp(_shared_collection_path)


def get_shared_collection(chroma_path):
    # one process-lifetime collection handle per chroma path, so a worker
    # thread or the watcher never opens a new PersistentClient (and its
    # ~83MB embedder) per note. path-keyed so a selftest can point at a
    # temp dir and get a fresh handle without reaching in to reset a global.
    #
    # get() reads sqlite and is fresh across processes, but vector search
    # reads a per-System hnsw reader that only sees its own process's writes -
    # and re-opening alone hands back the same cached System, so the
    # identifier cache has to be cleared first (measured, 14-08).
    global _shared_collection, _shared_collection_path, _shared_collection_stamp
    with _shared_refresh_lock:
        if _shared_collection is None or _shared_collection_path != chroma_path:
            _shared_collection = get_collection(chroma_path)
            _shared_collection_path = chroma_path
            _shared_collection_stamp = _chroma_stamp(chroma_path)
            return _shared_collection

        if _chroma_stamp(chroma_path) == _shared_collection_stamp:
            return _shared_collection

        SharedSystemClient.clear_system_cache()
        _shared_collection = get_collection(chroma_path)
        _shared_collection_stamp = _chroma_stamp(chroma_path)
        return _shared_collection


def upsert_note_chroma(note, source_path, collection):
    chunk_id = note_chunk_id(note.codice_fiscale, source_path)

    proc_text = ", ".join(note.procedures)
    if proc_text:
        text = f"Procedures: {proc_text}. {note.clinical_notes}"
    else:
        text = note.clinical_notes

    visit_date = note.visit_date.isoformat() if note.visit_date else ""

    collection.upsert(
        ids=[chunk_id],
        documents=[text],
        metadatas=[{
            "codice_fiscale": note.codice_fiscale,
            "patient_name": note.patient_name,
            "visit_date": visit_date,
            "source_path": source_path,
        }],
    )
    _note_own_write(collection)


def load_note(note, source_path, conn, collection, role, username):
    # loading a note into sqlite/chroma is appending clinical content -
    # gate it the same way agent.py gates a live append_note command
    if not authorize(role, "append_note"):
        log_audit(conn, username, role, "append_note", target=note.codice_fiscale, allowed=0)
        print(f"not permitted: {role} may not load notes")
        return
    log_audit(conn, username, role, "append_note", target=note.codice_fiscale, allowed=1)

    upsert_note_sql(note, source_path, conn)
    upsert_note_chroma(note, source_path, collection)


def note_chunk_id(codice_fiscale, source_path):
    return f"{codice_fiscale}:{Path(source_path).stem}"


def note_is_synced(note, source_path, conn, collection):
    # both halves must be present - sql alone is not synced (D-05)
    row = conn.execute(
        "SELECT 1 FROM visits WHERE source_path = ?", (source_path,)
    ).fetchone()
    if row is None:
        return False
    chunk_id = note_chunk_id(note.codice_fiscale, source_path)
    chunk = collection.get(ids=[chunk_id])
    return len(chunk["ids"]) > 0


def sync_note_file(json_path, sorted_root, conn, collection, role, username, target=None):
    json_path = Path(json_path)
    if target is None:
        target = str(json_path)

    reason = "verify-after-write says the note is not in both stores"
    try:
        note = DentalNote.model_validate_json(json_path.read_text())
        source_path = str(json_path.relative_to(sorted_root))
        before = note_is_synced(note, source_path, conn, collection)
        load_note(note, source_path, conn, collection, role, username)
        # check again after the write instead of trusting "no exception" -
        # this is what catches a denied role and a chroma half-write (D-05)
        ok = note_is_synced(note, source_path, conn, collection)
    except Exception as exc:
        ok = False
        reason = f"{type(exc).__name__}: {exc}"

    if not ok:
        # the audit row records that it failed; without this nothing anywhere
        # records why, and --backfill reports paths with no reasons either
        log_audit(conn, username, role, "sync_note", target, allowed=0)
        print(f"sync failed: {reason}", file=sys.stderr)
        return "failed"

    log_audit(conn, username, role, "sync_note", target, allowed=1)
    return "already" if before else "landed"


def save_new_note(note, conn, collection, role, username, sorted_root=Path("sorted")):
    # web-entered note, gated the same way a watcher-loaded note is. load_note
    # checks the role too, but a denied role must not get a file written for
    # it first, so the check is repeated here.
    if not authorize(role, "append_note"):
        log_audit(conn, username, role, "append_note", target=note.codice_fiscale, allowed=0)
        print(f"not permitted: {role} may not add notes")
        return "failed"

    # write the json file first - load_note/load_from_sorted assume it already
    # exists on disk, and this keeps a web-entered note indistinguishable from
    # a watcher-sorted one (same sorted/{cf}/notes tree, same file shape)
    filename = "web-" + datetime.now().isoformat().replace(":", "") + ".json"
    json_path = Path(sorted_root) / note.codice_fiscale / "notes" / filename
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(note.model_dump_json())

    # go through the shared path so this write gets the same verify-after-write
    # and the same sync_note outcome row as every other one (D-05)
    return sync_note_file(json_path, sorted_root, conn, collection, role, username)


def load_from_sorted(sorted_root, conn, collection, role, username):
    for json_path in sorted(Path(sorted_root).glob("*/notes/*.json")):
        note = DentalNote.model_validate_json(json_path.read_text())
        source_path = str(json_path.relative_to(sorted_root))
        load_note(note, source_path, conn, collection, role, username)


def _clear_failed_sync(conn, json_path):
    # a repair writes its outcome row as system, and the per-user intake list
    # filters system rows out by design - so "Not searchable" would never
    # clear for the person who uploaded the note. write the resolution under
    # their name instead. the uploader's row carries the .txt path (the worker
    # audits the routed file), the watcher's carries the .json.
    json_path = Path(json_path)
    row = conn.execute(
        "SELECT username, role, target, allowed FROM audit_log"
        " WHERE action = 'sync_note' AND role != 'system' AND target IN (?, ?)"
        " ORDER BY id DESC LIMIT 1",
        (str(json_path), str(json_path.with_suffix(".txt"))),
    ).fetchone()
    if row is not None and row["allowed"] == 0:
        log_audit(conn, row["username"], row["role"], "sync_note", row["target"], allowed=1)


def backfill_sorted(sorted_root, conn, collection, dry_run=False):
    # one-time repair for notes filed before this milestone (SYNC-02). runs
    # as system (D-03) - a bulk automated repair must never read as a
    # clinician appending hundreds of notes
    landed = 0
    already = 0
    failed = []

    for json_path in sorted(Path(sorted_root).glob("*/notes/*.json")):
        if dry_run:
            try:
                note = DentalNote.model_validate_json(json_path.read_text())
                source_path = str(json_path.relative_to(sorted_root))
                if note_is_synced(note, source_path, conn, collection):
                    already += 1
                else:
                    landed += 1
            except Exception:
                failed.append(str(json_path))
        else:
            outcome = sync_note_file(json_path, sorted_root, conn, collection, SYSTEM_ROLE, SYSTEM_USERNAME)
            if outcome == "failed":
                failed.append(str(json_path))
                continue
            if outcome == "landed":
                landed += 1
            else:
                already += 1
            _clear_failed_sync(conn, json_path)

    return landed, already, failed


def selftest():
    import subprocess
    import tempfile
    from dental_notes_schema import Invoice

    with tempfile.TemporaryDirectory() as tmp:
        conn = init_db(str(Path(tmp) / "clinic.sqlite"))

        tables = {row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert "patients" in tables, "1: patients table missing"
        assert "visits" in tables, "1: visits table missing"
        assert "invoices" in tables, "1: invoices table missing"
        assert "users" in tables, "1: users table missing"
        assert "audit_log" in tables, "1: audit_log table missing"
        assert "sessions" in tables, "1: sessions table missing"
        assert "pending_actions" in tables, "1: pending_actions table missing"

        fk_on = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk_on == 1, "1: foreign_keys pragma not ON"

        # the watcher writes this file from its own process while the app
        # reads it - the default rollback journal makes that a locked db
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert journal == "wal", f"1: expected WAL journal mode, got {journal}"

        # init_db must be safe to call twice on the same file
        conn2 = init_db(str(Path(tmp) / "clinic.sqlite"))
        tables2 = {row["name"] for row in conn2.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert tables2 == tables, "1: re-init changed table set"

        # users table gained the lockout columns, and the migration is idempotent
        user_columns = {row["name"] for row in conn2.execute("PRAGMA table_info(users)")}
        assert "failed_attempts" in user_columns, "1: failed_attempts column missing"
        assert "locked_until" in user_columns, "1: locked_until column missing"

        _ensure_lockout_columns(conn2)
        user_columns_again = {row["name"] for row in conn2.execute("PRAGMA table_info(users)")}
        assert user_columns_again == user_columns, "1: re-running lockout migration changed columns"

        # users.role is DB-enforced to exactly the three fixed roles
        try:
            conn.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                ("nurse_joe", "hash", "nurse"),
            )
            raise AssertionError("1: users.role accepted a role outside the fixed set")
        except sqlite3.IntegrityError:
            pass

        # 2. loading a note twice with the same source_path is idempotent
        cf = "MRRS800010150100"
        note = DentalNote(
            patient_name="mario rossi",
            codice_fiscale=cf,
            phone="333123456",
            visit_date=date(2026, 6, 1),
            procedures=["rct 26"],
            invoices=[Invoice(amount=50.0, description="rct")],
            clinical_notes="rct done",
        )
        upsert_note_sql(note, "MRRS800010150100/notes/n1.json", conn)
        upsert_note_sql(note, "MRRS800010150100/notes/n1.json", conn)

        patients_count = conn.execute("SELECT COUNT(*) c FROM patients").fetchone()["c"]
        visits_count = conn.execute("SELECT COUNT(*) c FROM visits").fetchone()["c"]
        invoices_count = conn.execute("SELECT COUNT(*) c FROM invoices").fetchone()["c"]
        assert patients_count == 1, f"2: expected 1 patient after 2 loads, got {patients_count}"
        assert visits_count == 1, f"2: expected 1 visit after 2 loads, got {visits_count}"
        assert invoices_count == len(note.invoices), \
            f"2: expected {len(note.invoices)} invoices after 2 loads, got {invoices_count}"

        # 3. lookup_patient returns name, phone, visit dates, and invoice rows
        result = lookup_patient(cf, conn)
        assert result["patient_name"] == "mario rossi", "3: wrong patient_name"
        assert result["phone"] == "333123456", "3: wrong phone"
        assert result["visit_dates"] == ["2026-06-01"], "3: wrong visit dates"
        assert result["invoices"] == [{"amount": 50.0, "description": "rct"}], "3: wrong invoices"

        # 4. a second note for the same CF, different source_path, adds a visit
        # and updates the patient in place rather than duplicating it
        note2 = DentalNote(
            patient_name="mario rossi",
            codice_fiscale=cf,
            phone="333999999",
            visit_date=date(2026, 6, 15),
            procedures=["cleaning"],
            clinical_notes="cleaning done",
        )
        upsert_note_sql(note2, "MRRS800010150100/notes/n2.json", conn)

        patients_count = conn.execute("SELECT COUNT(*) c FROM patients").fetchone()["c"]
        visits_count = conn.execute("SELECT COUNT(*) c FROM visits").fetchone()["c"]
        assert patients_count == 1, f"4: expected 1 patient after second note, got {patients_count}"
        assert visits_count == 2, f"4: expected 2 visits after second note, got {visits_count}"

        result2 = lookup_patient(cf, conn)
        assert result2["phone"] == "333999999", "4: patient phone not updated in place"

        # 5. chroma chunk is CF-tagged, 384-dim, and idempotent on re-upsert
        collection = get_collection(str(Path(tmp) / "chroma"))
        upsert_note_chroma(note, "MRRS800010150100/notes/n1.json", collection)
        upsert_note_chroma(note, "MRRS800010150100/notes/n1.json", collection)

        chunk_count = collection.count()
        assert chunk_count == 1, f"5: expected 1 chunk after 2 upserts, got {chunk_count}"

        chunks = collection.get(include=["embeddings", "metadatas"])
        assert chunks["ids"] == ["MRRS800010150100:n1"], f"5: wrong chunk id, got {chunks['ids']}"
        assert chunks["metadatas"][0]["codice_fiscale"] == cf, "5: chunk metadata CF mismatch"
        assert len(chunks["embeddings"][0]) == 384, \
            f"5: expected 384-dim embedding, got {len(chunks['embeddings'][0])}"

        # 6. load_from_sorted walks a real json tree for 2 patients, is idempotent,
        # and every loaded CF matches across sqlite and chroma (success criterion 4)
        sorted_root = Path(tmp) / "sorted"
        cf2 = "VRDL850315150200"
        note_a = DentalNote(
            patient_name="mario rossi",
            codice_fiscale=cf,
            phone="333123456",
            visit_date=date(2026, 6, 1),
            procedures=["rct 26"],
            invoices=[Invoice(amount=50.0, description="rct")],
            clinical_notes="rct done",
        )
        note_b = DentalNote(
            patient_name="luigi verdi",
            codice_fiscale=cf2,
            phone="333222222",
            visit_date=date(2026, 6, 2),
            clinical_notes="cleaning done",
        )
        for cf_, n in [(cf, note_a), (cf2, note_b)]:
            notes_dir = sorted_root / cf_ / "notes"
            notes_dir.mkdir(parents=True, exist_ok=True)
            (notes_dir / "n1.json").write_text(n.model_dump_json())

        db2_path = str(Path(tmp) / "clinic2.sqlite")
        conn2 = init_db(db2_path)
        collection2 = get_collection(str(Path(tmp) / "chroma2"))

        load_from_sorted(sorted_root, conn2, collection2, role="dentist", username="test-dentist")
        load_from_sorted(sorted_root, conn2, collection2, role="dentist", username="test-dentist")

        p_count = conn2.execute("SELECT COUNT(*) c FROM patients").fetchone()["c"]
        v_count = conn2.execute("SELECT COUNT(*) c FROM visits").fetchone()["c"]
        i_count = conn2.execute("SELECT COUNT(*) c FROM invoices").fetchone()["c"]
        assert p_count == 2, f"6: expected 2 patients after 2 runs, got {p_count}"
        assert v_count == 2, f"6: expected 2 visits after 2 runs, got {v_count}"
        assert i_count == 1, f"6: expected 1 invoice after 2 runs, got {i_count}"
        assert collection2.count() == 2, f"6: expected 2 chunks after 2 runs, got {collection2.count()}"

        for cf_ in [cf, cf2]:
            row = conn2.execute(
                "SELECT codice_fiscale FROM patients WHERE codice_fiscale = ?", (cf_,)
            ).fetchone()
            hits = collection2.get(where={"codice_fiscale": cf_})
            assert row is not None, f"6: sqlite missing patient {cf_}"
            assert len(hits["ids"]) == 1, f"6: chroma missing chunk for {cf_}"
            assert hits["metadatas"][0]["codice_fiscale"] == cf_, \
                f"6: chroma metadata CF mismatch for {cf_}"

        # 6b. a role without append_note (admin, D-01) writes nothing when
        # loading the same tree, and every attempted note is denied + audited
        db3_path = str(Path(tmp) / "clinic3.sqlite")
        conn3 = init_db(db3_path)
        collection3 = get_collection(str(Path(tmp) / "chroma3"))

        load_from_sorted(sorted_root, conn3, collection3, role="admin", username="test-admin")

        p_count3 = conn3.execute("SELECT COUNT(*) c FROM patients").fetchone()["c"]
        v_count3 = conn3.execute("SELECT COUNT(*) c FROM visits").fetchone()["c"]
        assert p_count3 == 0, f"6b: expected 0 patients loaded as admin, got {p_count3}"
        assert v_count3 == 0, f"6b: expected 0 visits loaded as admin, got {v_count3}"
        assert collection3.count() == 0, f"6b: expected 0 chroma chunks loaded as admin, got {collection3.count()}"

        denied_rows = conn3.execute(
            "SELECT * FROM audit_log WHERE action = 'append_note' AND allowed = 0"
        ).fetchall()
        assert len(denied_rows) == 2, f"6b: expected 2 denied append_note rows, got {len(denied_rows)}"

        # 7. re-importing a note whose invoice list shrank drops the stale rows,
        # so lookup never returns invoice amounts that no longer exist
        cf3 = "BNCS900010150300"
        note3 = DentalNote(
            patient_name="stefano bianchi",
            codice_fiscale=cf3,
            invoices=[
                Invoice(amount=10.0, description="a"),
                Invoice(amount=20.0, description="b"),
                Invoice(amount=30.0, description="c"),
            ],
        )
        upsert_note_sql(note3, "BNCS900010150300/notes/n1.json", conn)
        first_count = conn.execute(
            "SELECT COUNT(*) c FROM invoices WHERE codice_fiscale = ?", (cf3,)
        ).fetchone()["c"]
        assert first_count == 3, f"7: expected 3 invoices on first import, got {first_count}"

        note3_shrunk = DentalNote(
            patient_name="stefano bianchi",
            codice_fiscale=cf3,
            invoices=[Invoice(amount=10.0, description="a")],
        )
        upsert_note_sql(note3_shrunk, "BNCS900010150300/notes/n1.json", conn)
        after_count = conn.execute(
            "SELECT COUNT(*) c FROM invoices WHERE codice_fiscale = ?", (cf3,)
        ).fetchone()["c"]
        assert after_count == 1, f"7: expected 1 invoice after shrink re-import, got {after_count}"
        assert lookup_patient(cf3, conn)["invoices"] == [{"amount": 10.0, "description": "a"}], \
            "7: lookup returned phantom invoices after shrink re-import"

        # 8. save_new_note: authorized role writes a web-*.json file under
        # sorted/{cf}/notes/ AND a queryable sqlite row
        cf4 = "PLLM900010150400"
        note4 = DentalNote(
            patient_name="paolo lilli",
            codice_fiscale=cf4,
            visit_date=date(2026, 7, 1),
            clinical_notes="checkup done",
        )
        result4 = save_new_note(note4, conn, collection, "dentist", "test-dentist", sorted_root=sorted_root)

        note4_files = list((sorted_root / cf4 / "notes").glob("web-*.json"))
        assert len(note4_files) == 1, f"8: expected 1 web-*.json file, got {len(note4_files)}"
        assert lookup_patient(cf4, conn) is not None, "8: save_new_note left no sqlite row"
        assert result4 == "landed", f"8: expected landed, got {result4}"
        sync_rows4 = conn.execute(
            "SELECT * FROM audit_log WHERE action = 'sync_note' AND username = ? AND allowed = 1",
            ("test-dentist",),
        ).fetchall()
        assert len(sync_rows4) == 1, \
            f"8: web-entered note should get one sync_note outcome row, got {len(sync_rows4)}"

        # 8b. a denied role writes no file and logs an allowed=0 audit row
        cf5 = "BNCH900010150500"
        note5 = DentalNote(
            patient_name="bianca chen",
            codice_fiscale=cf5,
            clinical_notes="checkup done",
        )
        save_new_note(note5, conn, collection, "admin", "test-admin", sorted_root=sorted_root)

        assert not (sorted_root / cf5 / "notes").exists(), \
            "8b: admin should not be able to write a note file"
        assert lookup_patient(cf5, conn) is None, "8b: admin's denied note should not appear in sqlite"
        denied_note_rows = conn.execute(
            "SELECT * FROM audit_log WHERE username = ? AND action = 'append_note' AND allowed = 0",
            ("test-admin",),
        ).fetchall()
        assert len(denied_note_rows) == 1, \
            f"8b: expected 1 denied append_note row for test-admin, got {len(denied_note_rows)}"

        # 8c. a chroma failure on the web path is reported as a failed sync,
        # not raised out of the route with the sql half committed (D-05)
        class _FailingCollection8c:
            def upsert(self, **kwargs):
                raise RuntimeError("upsert boom")

            def get(self, ids=None):
                return {"ids": []}

        cf6 = "TSCF900010150700"
        note6 = DentalNote(
            patient_name="tosca fini",
            codice_fiscale=cf6,
            clinical_notes="checkup done",
        )
        result6 = save_new_note(
            note6, conn, _FailingCollection8c(), "dentist", "test-dentist", sorted_root=sorted_root
        )
        assert result6 == "failed", f"8c: expected failed, got {result6}"
        failed_sync_rows = conn.execute(
            "SELECT * FROM audit_log WHERE action = 'sync_note' AND username = ? AND allowed = 0",
            ("test-dentist",),
        ).fetchall()
        assert len(failed_sync_rows) == 1, \
            f"8c: a half-written web note should leave one failed sync_note row, got {len(failed_sync_rows)}"

        # 9. sync_note_file, system role: a filed note not yet in either store lands
        collection9 = get_collection(str(Path(tmp) / "chroma9"))
        cf9 = "GVNP900010150600"
        note9 = DentalNote(
            patient_name="giovanni pane",
            codice_fiscale=cf9,
            clinical_notes="sync test note",
        )
        notes_dir9 = sorted_root / cf9 / "notes"
        notes_dir9.mkdir(parents=True, exist_ok=True)
        json_path9 = notes_dir9 / "n1.json"
        json_path9.write_text(note9.model_dump_json())
        source_path9 = str(json_path9.relative_to(sorted_root))

        result9 = sync_note_file(json_path9, sorted_root, conn, collection9, SYSTEM_ROLE, SYSTEM_USERNAME)
        assert result9 == "landed", f"9: expected landed, got {result9}"
        assert lookup_patient(cf9, conn) is not None, "9: lookup_patient should find the synced patient"
        chunk_id9 = note_chunk_id(cf9, source_path9)
        assert collection9.get(ids=[chunk_id9])["ids"] == [chunk_id9], "9: chroma missing the synced chunk"
        sync_rows9 = conn.execute(
            "SELECT * FROM audit_log WHERE username = ? AND role = ? AND action = 'sync_note'",
            (SYSTEM_USERNAME, SYSTEM_ROLE),
        ).fetchall()
        assert len(sync_rows9) == 1, f"9: expected 1 sync_note row, got {len(sync_rows9)}"
        assert sync_rows9[0]["allowed"] == 1, "9: sync_note row should be allowed=1"
        append_rows9 = conn.execute(
            "SELECT * FROM audit_log WHERE username = ? AND role = ? AND action = 'append_note' AND allowed = 1",
            (SYSTEM_USERNAME, SYSTEM_ROLE),
        ).fetchall()
        assert len(append_rows9) == 1, \
            f"9: expected 1 allowed append_note row from load_note, got {len(append_rows9)}"

        # 9b. same call repeated is idempotent - no duplicate visit row or chunk
        result9b = sync_note_file(json_path9, sorted_root, conn, collection9, SYSTEM_ROLE, SYSTEM_USERNAME)
        assert result9b == "already", f"9b: expected already, got {result9b}"
        assert collection9.count() == 1, f"9b: expected chroma count unchanged at 1, got {collection9.count()}"
        visits9b = conn.execute(
            "SELECT COUNT(*) c FROM visits WHERE source_path = ?", (source_path9,)
        ).fetchone()["c"]
        assert visits9b == 1, f"9b: expected 1 visit row, got {visits9b}"

        # 9c. a role without append_note is denied and writes nothing (RBAC-05)
        cf9c = "GVNP900010150700"
        note9c = DentalNote(
            patient_name="test denied",
            codice_fiscale=cf9c,
            clinical_notes="denied sync test",
        )
        notes_dir9c = sorted_root / cf9c / "notes"
        notes_dir9c.mkdir(parents=True, exist_ok=True)
        json_path9c = notes_dir9c / "n1.json"
        json_path9c.write_text(note9c.model_dump_json())
        source_path9c = str(json_path9c.relative_to(sorted_root))

        result9c = sync_note_file(json_path9c, sorted_root, conn, collection9, "admin", "test-admin-sync")
        assert result9c == "failed", f"9c: expected failed, got {result9c}"
        visits9c = conn.execute(
            "SELECT COUNT(*) c FROM visits WHERE source_path = ?", (source_path9c,)
        ).fetchone()["c"]
        assert visits9c == 0, f"9c: expected no visit row for a denied sync, got {visits9c}"
        sync_rows9c = conn.execute(
            "SELECT * FROM audit_log WHERE username = ? AND action = 'sync_note' AND allowed = 0",
            ("test-admin-sync",),
        ).fetchall()
        assert len(sync_rows9c) == 1, f"9c: expected 1 denied sync_note row, got {len(sync_rows9c)}"

        # 9d. sql-ok/chroma-fail counts as failed, not landed (D-05). the sql
        # half is left in place so a later --backfill can repair it (D-04)
        class _ChromaDownStub:
            def upsert(self, **kwargs):
                raise RuntimeError("chroma down")

            def get(self, ids):
                return {"ids": []}

        cf9d = "GVNP900010150800"
        note9d = DentalNote(
            patient_name="test halfwrite",
            codice_fiscale=cf9d,
            clinical_notes="half write sync test",
        )
        notes_dir9d = sorted_root / cf9d / "notes"
        notes_dir9d.mkdir(parents=True, exist_ok=True)
        json_path9d = notes_dir9d / "n1.json"
        json_path9d.write_text(note9d.model_dump_json())
        source_path9d = str(json_path9d.relative_to(sorted_root))

        result9d = sync_note_file(json_path9d, sorted_root, conn, _ChromaDownStub(), SYSTEM_ROLE, SYSTEM_USERNAME)
        assert result9d == "failed", f"9d: expected failed, got {result9d}"
        sync_rows9d = conn.execute(
            "SELECT * FROM audit_log WHERE action = 'sync_note' AND allowed = 0 AND target = ?",
            (str(json_path9d),),
        ).fetchall()
        assert len(sync_rows9d) == 1, f"9d: expected 1 failed sync_note row, got {len(sync_rows9d)}"
        visits9d = conn.execute(
            "SELECT COUNT(*) c FROM visits WHERE source_path = ?", (source_path9d,)
        ).fetchone()["c"]
        assert visits9d == 1, \
            f"9d: expected the sql half to have written despite the chroma failure, got {visits9d}"

        # 9e. target override lets the upload worker share one audit target
        # with its upload_file row (plan 14-04)
        cf9e = "GVNP900010150900"
        note9e = DentalNote(
            patient_name="test target",
            codice_fiscale=cf9e,
            clinical_notes="target override sync test",
        )
        notes_dir9e = sorted_root / cf9e / "notes"
        notes_dir9e.mkdir(parents=True, exist_ok=True)
        json_path9e = notes_dir9e / "n1.json"
        json_path9e.write_text(note9e.model_dump_json())

        target9e = f"sorted/{cf9e}/notes/whatever.txt"
        result9e = sync_note_file(
            json_path9e, sorted_root, conn, collection9, SYSTEM_ROLE, SYSTEM_USERNAME, target=target9e
        )
        assert result9e == "landed", f"9e: expected landed, got {result9e}"
        sync_rows9e = conn.execute(
            "SELECT * FROM audit_log WHERE action = 'sync_note' AND target = ?", (target9e,)
        ).fetchall()
        assert len(sync_rows9e) == 1, \
            f"9e: expected the sync_note row target to be the override string, got {len(sync_rows9e)}"

        # 10. backfill_sorted (non-dry-run): two unsynced notes land, a second
        # run finds them already present, and no duplicate chroma chunks pile up
        sorted_root10 = Path(tmp) / "sorted10"
        conn10 = init_db(str(Path(tmp) / "clinic10.sqlite"))
        collection10 = get_collection(str(Path(tmp) / "chroma10"))

        cf10a = "GVNP900010151000"
        cf10b = "GVNP900010151100"
        note10a = DentalNote(patient_name="test backfill a", codice_fiscale=cf10a, clinical_notes="backfill test a")
        note10b = DentalNote(patient_name="test backfill b", codice_fiscale=cf10b, clinical_notes="backfill test b")
        for cf_, n in [(cf10a, note10a), (cf10b, note10b)]:
            notes_dir = sorted_root10 / cf_ / "notes"
            notes_dir.mkdir(parents=True, exist_ok=True)
            (notes_dir / "n1.json").write_text(n.model_dump_json())

        result10 = backfill_sorted(sorted_root10, conn10, collection10)
        assert result10 == (2, 0, []), f"10: expected (2, 0, []), got {result10}"

        result10_again = backfill_sorted(sorted_root10, conn10, collection10)
        assert result10_again == (0, 2, []), f"10: expected (0, 2, []) on second run, got {result10_again}"
        assert collection10.count() == 2, f"10: expected chroma count unchanged at 2, got {collection10.count()}"

        # 10b. dry_run=True classifies would-land notes but writes nothing -
        # safe to run against the live clinic database
        sorted_root10b = Path(tmp) / "sorted10b"
        conn10b = init_db(str(Path(tmp) / "clinic10b.sqlite"))
        collection10b = get_collection(str(Path(tmp) / "chroma10b"))

        cf10c_ = "GVNP900010151200"
        note10c_ = DentalNote(patient_name="test dry run", codice_fiscale=cf10c_, clinical_notes="dry run test")
        notes_dir10b = sorted_root10b / cf10c_ / "notes"
        notes_dir10b.mkdir(parents=True, exist_ok=True)
        (notes_dir10b / "n1.json").write_text(note10c_.model_dump_json())

        result10b = backfill_sorted(sorted_root10b, conn10b, collection10b, dry_run=True)
        assert result10b == (1, 0, []), f"10b: expected (1, 0, []), got {result10b}"

        visits10b = conn10b.execute("SELECT COUNT(*) c FROM visits").fetchone()["c"]
        audit10b = conn10b.execute("SELECT COUNT(*) c FROM audit_log").fetchone()["c"]
        assert visits10b == 0, f"10b: expected 0 visits after dry run, got {visits10b}"
        assert audit10b == 0, f"10b: expected 0 audit_log rows after dry run, got {audit10b}"
        assert collection10b.count() == 0, f"10b: expected 0 chroma chunks after dry run, got {collection10b.count()}"

        # 10c. a corrupt json fails and is listed by path; the walk continues
        # to the remaining files in the same tree
        sorted_root10c = Path(tmp) / "sorted10c"
        conn10c = init_db(str(Path(tmp) / "clinic10c.sqlite"))
        collection10c = get_collection(str(Path(tmp) / "chroma10c"))

        cf10d = "GVNP900010151300"
        note10d = DentalNote(patient_name="test ok note", codice_fiscale=cf10d, clinical_notes="ok note")
        notes_dir10d = sorted_root10c / cf10d / "notes"
        notes_dir10d.mkdir(parents=True, exist_ok=True)
        (notes_dir10d / "n1.json").write_text(note10d.model_dump_json())

        broken_dir = sorted_root10c / "BROKEN000000000000" / "notes"
        broken_dir.mkdir(parents=True, exist_ok=True)
        broken_path = broken_dir / "broken.json"
        broken_path.write_text("not json at all")

        landed10c, already10c, failed10c = backfill_sorted(sorted_root10c, conn10c, collection10c)
        assert landed10c == 1, f"10c: expected 1 landed note, got {landed10c}"
        assert str(broken_path) in failed10c, f"10c: expected broken path in failed list, got {failed10c}"
        assert lookup_patient(cf10d, conn10c) is not None, "10c: the valid note in the same tree should still land"

        # 10d. a repair has to clear the uploader's "Not searchable" state. its
        # own outcome row is written as system, and the per-user intake list
        # filters system rows out, so the resolution needs their name on it.
        sorted_root10d = Path(tmp) / "sorted10d"
        conn10d = init_db(str(Path(tmp) / "clinic10d.sqlite"))
        collection10d = get_collection(str(Path(tmp) / "chroma10d"))

        cf10e = "GVNP900010151400"
        note10e = DentalNote(
            patient_name="renata pini", codice_fiscale=cf10e, clinical_notes="note to repair"
        )
        notes_dir10e = sorted_root10d / cf10e / "notes"
        notes_dir10e.mkdir(parents=True, exist_ok=True)
        (notes_dir10e / "n1.json").write_text(note10e.model_dump_json())

        # the worker audits the routed .txt, which is what the badge reads
        txt_target10e = str(notes_dir10e / "n1.txt")
        log_audit(conn10d, "drossi", "dentist", "sync_note", txt_target10e, allowed=0)

        backfill_sorted(sorted_root10d, conn10d, collection10d)

        newest10e = conn10d.execute(
            "SELECT * FROM audit_log WHERE action = 'sync_note' AND target = ? ORDER BY id DESC LIMIT 1",
            (txt_target10e,),
        ).fetchone()
        assert newest10e["allowed"] == 1, "10d: the uploader's failed sync row was never resolved"
        assert newest10e["username"] == "drossi", \
            f"10d: resolution not attributed to the uploader, got {newest10e['username']}"
        assert newest10e["role"] != SYSTEM_ROLE, \
            "10d: a system-role resolution is filtered out of the user's intake list"

        # 11. cross-process visibility - a chunk written by a real second
        # process (the watcher, --backfill) must be found by query() without
        # restarting this process (DEFECT-1, 14-08)
        path11 = Path(tmp) / "chroma11"
        collection11 = get_shared_collection(str(path11))
        cf11 = "BNCG800010150400"
        note11 = DentalNote(
            patient_name="giorgio bianchi",
            codice_fiscale=cf11,
            clinical_notes="fitted with a night guard",
        )
        upsert_note_chroma(note11, f"{cf11}/notes/n1.json", collection11)
        seeded = collection11.query(query_texts=["night guard"], n_results=3)
        assert f"{cf11}:n1" in seeded["ids"][0], \
            f"11: seeded chunk not found by query, got {seeded['ids'][0]}"

        writer_code = (
            "import storage\n"
            f"c = storage.get_collection({str(path11)!r})\n"
            "c.upsert(\n"
            "    ids=['EXTERNAL:one'],\n"
            "    documents=['fitted a night guard splint'],\n"
            f"    metadatas=[{{'codice_fiscale': {cf11!r}}}],\n"
            ")\n"
        )
        subprocess.run(
            [sys.executable, "-c", writer_code],
            check=True,
            cwd=str(Path(__file__).parent),
        )

        refreshed = get_shared_collection(str(path11))
        found = refreshed.query(query_texts=["night guard splint"], n_results=5)
        assert "EXTERNAL:one" in found["ids"][0], \
            f"11: externally written chunk not visible after refresh, got {found['ids'][0]}"

        # 11b. no churn on the common path - repeat calls with no external
        # write in between must return the identical cached object
        assert get_shared_collection(str(path11)) is get_shared_collection(str(path11)), \
            "11b: unconditional re-open on the common path"

        # 11c. an external in-place re-upsert leaves the row count alone -
        # exactly what --backfill does to an already-present note - so a
        # count-equality gate would keep searching the pre-repair text
        stale_id = f"{cf11}:n1"
        repaired_text = "root canal on the upper left molar"
        rewriter_code = (
            "import storage\n"
            f"c = storage.get_collection({str(path11)!r})\n"
            "c.upsert(\n"
            f"    ids=[{stale_id!r}],\n"
            f"    documents=[{repaired_text!r}],\n"
            f"    metadatas=[{{'codice_fiscale': {cf11!r}}}],\n"
            ")\n"
        )
        count_before = get_shared_collection(str(path11)).count()
        subprocess.run(
            [sys.executable, "-c", rewriter_code],
            check=True,
            cwd=str(Path(__file__).parent),
        )

        rewritten = get_shared_collection(str(path11))
        assert rewritten.count() == count_before, \
            "11c: the rewrite was supposed to leave the count unchanged"
        found11c = rewritten.query(query_texts=[repaired_text], n_results=1)
        assert found11c["ids"][0][0] == stale_id, \
            f"11c: repaired chunk not ranked first, got {found11c['ids'][0]}"
        assert found11c["distances"][0][0] < 0.01, \
            f"11c: query ran against the stale embedding, distance {found11c['distances'][0][0]}"

        # 11d. this process's own write must not force a rebuild - the
        # replacement client would load another ~83MB embedder alongside it
        note11d = DentalNote(
            patient_name="giorgio bianchi",
            codice_fiscale=cf11,
            clinical_notes="scale and polish",
        )
        upsert_note_chroma(note11d, f"{cf11}/notes/n2.json", rewritten)
        assert get_shared_collection(str(path11)) is rewritten, \
            "11d: an in-process write forced a rebuild of the shared handle"

    print("selftest ok")


def main():
    # usage: python storage.py [--selftest | --backfill [--dry-run]]
    # bare invocation (no flags) loads sorted/ as the logged-in cli session,
    # unchanged from before this repair command existed
    flags = [a for a in sys.argv[1:] if a.startswith("--")]

    # a typo used to be silently ignored, so "--dry_run" ran a real backfill
    known = {"--selftest", "--backfill", "--dry-run"}
    unknown = [f for f in flags if f not in known]
    if unknown:
        print(f"unknown option: {unknown[0]}")
        sys.exit(1)

    if "--selftest" in flags:
        selftest()
        return

    if "--backfill" in flags:
        # runs as system (D-03), not the operator's cli session - a bulk
        # repair must never read as a clinician appending hundreds of notes
        dry_run = "--dry-run" in flags
        Path("db").mkdir(exist_ok=True)
        conn = init_db("db/clinic.sqlite")
        collection = get_shared_collection("db/chroma")
        landed, already, failed = backfill_sorted(Path("sorted"), conn, collection, dry_run=dry_run)

        if dry_run:
            print("dry run - nothing written")
        print(f"landed {landed}, already present {already}, failed {len(failed)}")
        for path in failed:
            print(f"    {path}")
        return

    from cli_session import read_session

    session = read_session()
    if session is None:
        print("not logged in - run: python cli_session.py login")
        return

    Path("db").mkdir(exist_ok=True)
    conn = init_db("db/clinic.sqlite")
    collection = get_collection("db/chroma")
    load_from_sorted(Path("sorted"), conn, collection, session["role"], session["username"])

    patients = conn.execute("SELECT COUNT(*) c FROM patients").fetchone()["c"]
    print(f"loaded {patients} patients, {collection.count()} chroma chunks")


if __name__ == "__main__":
    main()
