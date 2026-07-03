import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

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
    """)
    conn.commit()
    return conn


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

    for i, inv in enumerate(note.invoices):
        conn.execute("""
            INSERT INTO invoices (codice_fiscale, visit_id, line_index, amount, description)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(visit_id, line_index) DO UPDATE SET
                amount = excluded.amount,
                description = excluded.description
        """, (note.codice_fiscale, visit_id, i, inv.amount, inv.description))

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

        fk_on = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk_on == 1, "1: foreign_keys pragma not ON"

        # init_db must be safe to call twice on the same file
        conn2 = init_db(str(Path(tmp) / "clinic.sqlite"))
        tables2 = {row["name"] for row in conn2.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert tables2 == tables, "1: re-init changed table set"

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

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python storage.py --selftest")
    sys.exit(1)


if __name__ == "__main__":
    main()
