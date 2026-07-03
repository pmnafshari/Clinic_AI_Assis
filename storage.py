import sqlite3
import sys
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


def selftest():
    import tempfile

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

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python storage.py --selftest")
    sys.exit(1)


if __name__ == "__main__":
    main()
