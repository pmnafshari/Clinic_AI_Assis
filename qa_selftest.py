import json
import re
import sqlite3
import sys
import tempfile
from datetime import date
from pathlib import Path

from werkzeug.security import generate_password_hash

import app.db as app_db
import app.qa_routes as qa_routes
from app import create_app
from dental_notes_schema import DentalNote
from storage import upsert_note_sql


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
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def read(self):
            return json.dumps({"response": "Not in records."}).encode()

    return FakeResponse()


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "clinic.sqlite")
        app_db.DB_PATH = db_path
        qa_routes.CHROMA_PATH = str(Path(tmp) / "chroma")

        app = create_app()
        app.config["TESTING"] = True
        qa_routes._urlopen = fake_urlopen

        # SC3 - anonymous GET /qa redirects to /login, never reaches the page
        client_anon = app.test_client()
        anon_resp = client_anon.get("/qa")
        assert anon_resp.status_code == 302, "SC3: anonymous GET /qa should redirect"
        assert "/login" in anon_resp.headers["Location"], "SC3: redirect target should be /login"

        _seed_user(db_path, "drossi", "goodpass", "dentist")
        _seed_user(db_path, "aadmin", "goodpass", "admin")

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cf = "RSSM800010150100"
        note = DentalNote(
            patient_name="mario rossi",
            codice_fiscale=cf,
            phone="333123456",
            visit_date=date(2026, 6, 1),
            clinical_notes="rct done",
        )
        upsert_note_sql(note, "RSSM800010150100/notes/n1.json", conn)

        # two patients sharing a surname, for the multi-cf disambiguation test
        cf3 = "BNCP900010150300"
        cf4 = "BNCC910010150400"
        upsert_note_sql(
            DentalNote(patient_name="paola bianchi", codice_fiscale=cf3),
            "BNCP900010150300/notes/n1.json", conn,
        )
        upsert_note_sql(
            DentalNote(patient_name="carlo bianchi", codice_fiscale=cf4),
            "BNCC910010150400/notes/n1.json", conn,
        )
        conn.close()

        # log in as dentist
        client = app.test_client()
        login_get = client.get("/login")
        csrf = _csrf_from(login_get.text)
        login_resp = client.post(
            "/login", data={"username": "drossi", "password": "goodpass", "csrf_token": csrf}
        )
        assert login_resp.status_code == 302, "dentist login should redirect"

        # SC1 - exact question returns an answer with a [source: ...] citation
        qa_get = client.get("/qa")
        assert qa_get.status_code == 200, "SC1: GET /qa should return 200 for a dentist"
        csrf_qa = _csrf_from(qa_get.text)
        exact_resp = client.post(
            "/qa",
            data={"question": "What is patient Rossi's phone number?", "csrf_token": csrf_qa},
        )
        assert exact_resp.status_code == 200, "SC1: exact question POST should return 200"
        assert "333123456" in exact_resp.text, "SC1: answer should contain the phone value"
        assert "[source:" in exact_resp.text, "SC1: answer should carry a source citation"

        # SC2 - meaning question with no matching record returns the guard, no citation
        csrf_qa2 = _csrf_from(exact_resp.text)
        meaning_resp = client.post(
            "/qa",
            data={"question": "which patients had a root canal?", "csrf_token": csrf_qa2},
        )
        assert meaning_resp.status_code == 200, "SC2: meaning question POST should return 200"
        assert "not in records" in meaning_resp.text, "SC2: guard string missing"
        assert "[source:" not in meaning_resp.text, "SC2: guard answer must carry no citation"

        # multi-cf - shared surname renders a candidate picker
        csrf_qa3 = _csrf_from(meaning_resp.text)
        multi_resp = client.post(
            "/qa",
            data={"question": "what is patient Bianchi's phone number?", "csrf_token": csrf_qa3},
        )
        assert multi_resp.status_code == 200, "multi-cf: POST should return 200"
        assert "pick one:" in multi_resp.text, "multi-cf: candidate prompt missing"
        assert 'name="cf"' in multi_resp.text, "multi-cf: cf radio inputs missing"

        # re-post with the chosen cf returns the exact answer for that patient
        csrf_qa4 = _csrf_from(multi_resp.text)
        chosen_resp = client.post(
            "/qa",
            data={
                "question": "what is patient Bianchi's phone number?",
                "cf": cf3,
                "csrf_token": csrf_qa4,
            },
        )
        assert chosen_resp.status_code == 200, "multi-cf: re-POST should return 200"
        assert "paola bianchi" in chosen_resp.text.lower(), \
            "multi-cf: re-POST should answer for the chosen patient"

        # admin denial - no read_notes permission, denial written to audit_log
        client_admin = app.test_client()
        admin_login_get = client_admin.get("/login")
        csrf_admin = _csrf_from(admin_login_get.text)
        admin_login_resp = client_admin.post(
            "/login", data={"username": "aadmin", "password": "goodpass", "csrf_token": csrf_admin}
        )
        assert admin_login_resp.status_code == 302, "admin login should redirect"

        admin_qa_get = client_admin.get("/qa")
        assert admin_qa_get.status_code == 200, "admin GET /qa should render (denied, not 404)"
        assert "permission to view clinical records" in admin_qa_get.text, \
            "admin: denial message missing on GET"

        csrf_admin_qa = _csrf_from(admin_qa_get.text)
        admin_resp = client_admin.post(
            "/qa",
            data={"question": "What is patient Rossi's phone number?", "csrf_token": csrf_admin_qa},
        )
        assert admin_resp.status_code == 200, "admin POST /qa should render (denied, not 404)"
        assert "permission to view clinical records" in admin_resp.text, \
            "admin: denial message missing on POST"

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM audit_log WHERE username = ? AND action = 'read_notes' AND allowed = 0",
            ("aadmin",),
        ).fetchone()
        conn.close()
        assert row is not None, "admin: denied read_notes attempt should be audited"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python qa_selftest.py --selftest")


if __name__ == "__main__":
    main()
