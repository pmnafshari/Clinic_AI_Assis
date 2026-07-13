import json
import re
import sqlite3
import sys
import tempfile
import urllib.error
from pathlib import Path

from werkzeug.security import generate_password_hash

import app.db as app_db
import app.notes_routes as notes_routes
from app import create_app

FAKE_CF = "PLLM900010150400"

_call_count = {"n": 0}


def _seed_user(db_path, username, password, role):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO users (username, password_hash, role, active) VALUES (?, ?, ?, 1)",
        (username, generate_password_hash(password), role),
    )
    conn.commit()
    conn.close()


def _csrf_from(html):
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return match.group(1)


def fake_urlopen(req, timeout=120):
    _call_count["n"] += 1

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def read(self):
            note = {
                "patient_name": "paolo lilli",
                "codice_fiscale": FAKE_CF,
                "phone": "333123456",
                "visit_date": "2026-06-01",
                "procedures": ["cleaning"],
                "invoices": [{"amount": 250.0, "description": "rct 26"}],
                "clinical_notes": "routine checkup",
                "next_appointment": "6m",
            }
            return json.dumps({"response": json.dumps(note)}).encode()

    return FakeResponse()


def boom_urlopen(req, timeout=120):
    raise urllib.error.URLError("connection refused")


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "clinic.sqlite")
        app_db.DB_PATH = db_path
        notes_routes.CHROMA_PATH = str(Path(tmp) / "chroma")
        notes_routes.SORTED_ROOT = Path(tmp) / "sorted"

        app = create_app()
        app.config["TESTING"] = True
        notes_routes._urlopen = fake_urlopen

        _seed_user(db_path, "drossi", "goodpass", "dentist")
        _seed_user(db_path, "aassist", "goodpass", "assistant")
        _seed_user(db_path, "aadmin", "goodpass", "admin")

        # log in as dentist
        client = app.test_client()
        login_get = client.get("/login")
        csrf = _csrf_from(login_get.text)
        login_resp = client.post(
            "/login", data={"username": "drossi", "password": "goodpass", "csrf_token": csrf}
        )
        assert login_resp.status_code == 302, "dentist login should redirect"

        # 1/2. GUI-02 happy path - paste raises an editable preview, no write yet,
        # extract_note is called exactly once for the whole two-step flow
        assert _call_count["n"] == 0, "extract_note should not have run yet"

        new_get = client.get("/notes/new")
        assert new_get.status_code == 200, "GET /notes/new should return 200"
        csrf_paste = _csrf_from(new_get.text)

        paste_resp = client.post(
            "/notes/new", data={"raw_note": "paolo came in, checkup", "csrf_token": csrf_paste}
        )
        assert paste_resp.status_code == 200, "raw_note POST should return 200"
        assert _call_count["n"] == 1, "extract_note should have run exactly once after step 1"
        assert 'value="paolo lilli"' in paste_resp.text, "preview should show extracted patient name"
        assert f'value="{FAKE_CF}"' in paste_resp.text, "preview should show extracted codice fiscale"
        assert 'name="patient_name"' in paste_resp.text, "preview form fields should be editable inputs"
        assert 'value="2026-06-01"' in paste_resp.text, "preview should show the extracted visit date"
        assert 'name="invoice_amount"' in paste_resp.text, "preview should carry the extracted invoice"
        assert "rct 26" in paste_resp.text, "preview should show the invoice description"

        assert not (Path(tmp) / "sorted" / FAKE_CF).exists(), "no write should happen before confirm"

        csrf_preview = _csrf_from(paste_resp.text)
        save_resp = client.post(
            "/notes/new",
            data={
                "patient_name": "paolo lilli",
                "codice_fiscale": FAKE_CF,
                "phone": "333999999",  # staff-corrected phone
                "visit_date": "2026-06-01",
                "clinical_notes": "routine checkup",
                "procedures": "cleaning",
                "invoice_amount": "250.0",
                "invoice_description": "rct 26",
                "next_appointment": "6m",
                "csrf_token": csrf_preview,
            },
        )
        assert save_resp.status_code == 302, "corrected-fields POST should redirect on success"
        assert _call_count["n"] == 1, "extract_note must not re-run on the confirm/submit step"

        note_files = list((Path(tmp) / "sorted" / FAKE_CF / "notes").glob("web-*.json"))
        assert len(note_files) == 1, f"expected 1 web-*.json file, got {len(note_files)}"

        stored = json.loads(note_files[0].read_text())
        assert stored["visit_date"] == "2026-06-01", "saved json should keep the extracted visit date"
        assert stored["invoices"] == [{"amount": 250.0, "description": "rct 26"}], \
            "saved json should keep the extracted invoice"

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        patient_row = conn.execute(
            "SELECT * FROM patients WHERE codice_fiscale = ?", (FAKE_CF,)
        ).fetchone()
        conn.close()
        assert patient_row is not None, "corrected-fields submit should write a queryable sqlite row"
        assert patient_row["phone"] == "333999999", "sqlite row should carry the corrected phone"

        # 3. denied role - a role lacking append_note is refused server-side,
        # audited allowed=0, and writes no file
        client_admin = app.test_client()
        admin_login_get = client_admin.get("/login")
        csrf_admin = _csrf_from(admin_login_get.text)
        admin_login_resp = client_admin.post(
            "/login", data={"username": "aadmin", "password": "goodpass", "csrf_token": csrf_admin}
        )
        assert admin_login_resp.status_code == 302, "admin login should redirect"

        admin_new_get = client_admin.get("/notes/new")
        csrf_admin_notes = _csrf_from(admin_new_get.text)

        denied_cf = "BNCH900010150500"
        denied_resp = client_admin.post(
            "/notes/new",
            data={
                "patient_name": "bianca chen",
                "codice_fiscale": denied_cf,
                "phone": "",
                "clinical_notes": "checkup done",
                "procedures": "",
                "next_appointment": "",
                "csrf_token": csrf_admin_notes,
            },
        )
        assert denied_resp.status_code == 200, "denied submit should re-render, not redirect"
        assert "permission to add notes" in denied_resp.text, "denied submit should show the permission message"

        assert not (Path(tmp) / "sorted" / denied_cf).exists(), "denied role should write no file"

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        denied_row = conn.execute(
            "SELECT * FROM audit_log WHERE username = ? AND action = 'append_note' AND allowed = 0",
            ("aadmin",),
        ).fetchone()
        conn.close()
        assert denied_row is not None, "denied submit should log an allowed=0 audit_log row"

        # 4. Ollama-down - a raw_note POST re-renders the friendly error, never a 500
        notes_routes._urlopen = boom_urlopen
        down_new_get = client.get("/notes/new")
        csrf_down = _csrf_from(down_new_get.text)
        down_resp = client.post(
            "/notes/new", data={"raw_note": "another note", "csrf_token": csrf_down}
        )
        assert down_resp.status_code == 200, "Ollama-down should re-render, not 500"
        assert "ollama run dental-notes" in down_resp.text.lower(), \
            "Ollama-down should surface the remediation message"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python notes_selftest.py --selftest")


if __name__ == "__main__":
    main()
