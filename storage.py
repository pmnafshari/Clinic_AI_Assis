import json
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

import chromadb
from chromadb.config import Settings

from auth import authorize, log_audit
from dental_notes_schema import DentalNote


def init_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
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
            active INTEGER NOT NULL DEFAULT 1
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
        "SELECT visit_date, procedures, clinical_notes, next_appointment"
        " FROM visits WHERE codice_fiscale = ? ORDER BY id", (cf,)
    ).fetchall()

    return [
        {
            "visit_date": v["visit_date"],
            "procedures": json.loads(v["procedures"]) if v["procedures"] else [],
            "clinical_notes": v["clinical_notes"],
            "next_appointment": v["next_appointment"],
        }
        for v in visits
    ]


def get_collection(chroma_path):
    # first run downloads the ~83MB all-MiniLM-L6-v2 ONNX model once, then it's cached
    client = chromadb.PersistentClient(
        path=chroma_path,
        settings=Settings(anonymized_telemetry=False),
    )
    return client.get_or_create_collection(name="patient_notes")


def upsert_note_chroma(note, source_path, collection):
    stem = Path(source_path).stem
    chunk_id = f"{note.codice_fiscale}:{stem}"

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


def save_new_note(note, conn, collection, role, username, sorted_root=Path("sorted")):
    # web-entered note, gated the same way a watcher-loaded note is
    if not authorize(role, "append_note"):
        log_audit(conn, username, role, "append_note", target=note.codice_fiscale, allowed=0)
        print(f"not permitted: {role} may not add notes")
        return
    log_audit(conn, username, role, "append_note", target=note.codice_fiscale, allowed=1)

    # write the json file first - load_note/load_from_sorted assume it already
    # exists on disk, and this keeps a web-entered note indistinguishable from
    # a watcher-sorted one (same sorted/{cf}/notes tree, same file shape)
    filename = "web-" + datetime.now().isoformat().replace(":", "") + ".json"
    json_path = Path(sorted_root) / note.codice_fiscale / "notes" / filename
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(note.model_dump_json())

    source_path = str(json_path.relative_to(sorted_root))
    upsert_note_sql(note, source_path, conn)
    upsert_note_chroma(note, source_path, collection)


def load_from_sorted(sorted_root, conn, collection, role, username):
    for json_path in sorted(Path(sorted_root).glob("*/notes/*.json")):
        note = DentalNote.model_validate_json(json_path.read_text())
        source_path = str(json_path.relative_to(sorted_root))
        load_note(note, source_path, conn, collection, role, username)


def selftest():
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
        save_new_note(note4, conn, collection, "dentist", "test-dentist", sorted_root=sorted_root)

        note4_files = list((sorted_root / cf4 / "notes").glob("web-*.json"))
        assert len(note4_files) == 1, f"8: expected 1 web-*.json file, got {len(note4_files)}"
        assert lookup_patient(cf4, conn) is not None, "8: save_new_note left no sqlite row"

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

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
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
