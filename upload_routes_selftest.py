import io
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from werkzeug.security import generate_password_hash

import app.db as app_db
import app.upload_routes as upload_routes
import upload_worker
from app import create_app
from extract_note import OllamaUnreachable

VALID_CF = "MRRS800010150100"


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


def _login(app, username, password):
    client = app.test_client()
    get_resp = client.get("/login")
    csrf = _csrf_from(get_resp.text)
    login_resp = client.post(
        "/login", data={"username": username, "password": password, "csrf_token": csrf}
    )
    assert login_resp.status_code == 302, f"login for {username} should redirect"
    return client


def _audit_rows(db_path, **where):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    clauses = " AND ".join(f"{k} = ?" for k in where)
    rows = conn.execute(
        f"SELECT * FROM audit_log WHERE {clauses}", tuple(where.values())
    ).fetchall()
    conn.close()
    return rows


def _wait_for_needs_review(db_path, username, deadline_seconds=3.0):
    deadline = time.time() + deadline_seconds
    while time.time() < deadline:
        rows = _audit_rows(db_path, username=username, action="upload_file", allowed=1)
        for row in rows:
            if row["target"] and "needs_review" in row["target"]:
                return row
        time.sleep(0.1)
    return None


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = str(tmp_path / "clinic.sqlite")
        sorted_root = tmp_path / "sorted"
        drop_dir = tmp_path / "drop"
        log_path = str(tmp_path / "sorted" / "log.txt")

        app_db.DB_PATH = db_path
        app_db.CHROMA_PATH = str(tmp_path / "chroma")
        app_db._collection_cache = None

        upload_routes.SORTED_ROOT = sorted_root
        upload_routes.DROP_DIR = drop_dir
        upload_routes.LOG_PATH = log_path
        upload_worker.SORTED_ROOT = sorted_root
        upload_worker.DB_PATH = db_path
        upload_worker.LOG_PATH = log_path

        app = create_app()
        app.config["TESTING"] = True

        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO patients (codice_fiscale, patient_name, phone) VALUES (?, ?, ?)",
            (VALID_CF, "mario rossi", "333123456"),
        )
        conn.commit()
        conn.close()

        _seed_user(db_path, "drossi", "goodpass", "dentist")
        _seed_user(db_path, "aassist", "goodpass", "assistant")
        _seed_user(db_path, "aadmin", "goodpass", "admin")

        dentist_client = _login(app, "drossi", "goodpass")
        assistant_client = _login(app, "aassist", "goodpass")
        admin_client = _login(app, "aadmin", "goodpass")

        # 1. SC1 - dentist uploads an xlsx, it lands under <CF>/records, and
        # an allowed=1 audit row is written (CF-prefix injection, D-15)
        detail_resp = dentist_client.get(f"/patients/{VALID_CF}")
        csrf1 = _csrf_from(detail_resp.text)
        upload_resp = dentist_client.post(
            f"/patients/{VALID_CF}/upload",
            data={"csrf_token": csrf1, "files": (io.BytesIO(b"PK"), "invoice.xlsx")},
            content_type="multipart/form-data",
            headers={"HX-Request": "true"},
        )
        assert upload_resp.status_code == 200, "1: xlsx upload should return the toast fragment"
        assert "Filed to records" in upload_resp.text, "1: toast should report the records folder"
        records_dir = sorted_root / VALID_CF / "records"
        assert records_dir.is_dir() and any(records_dir.iterdir()), \
            "1: xlsx did not land under <CF>/records"
        rows1 = _audit_rows(db_path, username="drossi", action="upload_file", allowed=1)
        assert len(rows1) == 1, f"1: expected 1 allowed audit row for drossi, got {len(rows1)}"

        # 2. SC1 pdf - lands under <CF>/documents
        detail_resp2 = dentist_client.get(f"/patients/{VALID_CF}")
        csrf2 = _csrf_from(detail_resp2.text)
        dentist_client.post(
            f"/patients/{VALID_CF}/upload",
            data={"csrf_token": csrf2, "files": (io.BytesIO(b"%PDF"), "xray.pdf")},
            content_type="multipart/form-data",
            headers={"HX-Request": "true"},
        )
        documents_dir = sorted_root / VALID_CF / "documents"
        assert documents_dir.is_dir() and any(documents_dir.iterdir()), \
            "2: pdf did not land under <CF>/documents"

        # 3. SC2 rejected - a disallowed extension is refused and never
        # written to drop/ or sorted/ (D-08)
        detail_resp3 = dentist_client.get(f"/patients/{VALID_CF}")
        csrf3 = _csrf_from(detail_resp3.text)
        reject_resp = dentist_client.post(
            f"/patients/{VALID_CF}/upload",
            data={"csrf_token": csrf3, "files": (io.BytesIO(b"bad"), "malware.dat")},
            content_type="multipart/form-data",
            headers={"HX-Request": "true"},
        )
        assert reject_resp.status_code == 200
        assert "not allowed" in reject_resp.text.lower(), "3: .dat should be reported as rejected"
        dat_files = list(drop_dir.rglob("*.dat")) + list(sorted_root.rglob("*.dat"))
        assert not dat_files, f"3: a rejected .dat must never be written to disk, found {dat_files}"

        # 4. SC2 generic fallback - an allow-listed image with no cf match
        # sorts to the top-level images/ folder. this is a successful generic
        # categorization, NOT a needs_review outcome.
        dashboard_resp = dentist_client.get("/")
        csrf4 = _csrf_from(dashboard_resp.text)
        generic_resp = dentist_client.post(
            "/upload",
            data={"csrf_token": csrf4, "files": (io.BytesIO(b"\xff\xd8\xff"), "xray_generic.png")},
            content_type="multipart/form-data",
            headers={"HX-Request": "true"},
        )
        assert generic_resp.status_code == 200
        assert "Filed to images" in generic_resp.text, \
            "4: a no-cf image should report a generic-fallback sort, not needs_review"
        assert (sorted_root / "images" / "xray_generic.png").exists(), \
            "4: no-cf image did not land in the top-level images/ folder"

        # 5. SC2 needs_review - the real needs_review path is a .txt whose
        # extraction fails (offline Ollama), not the generic-fallback path
        # above. patch upload_worker._extract to fail deterministically.
        orig_extract = upload_worker._extract

        def _fake_unreachable(text):
            raise OllamaUnreachable("offline")

        upload_worker._extract = _fake_unreachable
        try:
            dashboard_resp2 = assistant_client.get("/")
            csrf5 = _csrf_from(dashboard_resp2.text)
            queued_resp = assistant_client.post(
                "/upload",
                data={"csrf_token": csrf5, "files": (io.BytesIO(b"some note text"), "note.txt")},
                content_type="multipart/form-data",
                headers={"HX-Request": "true"},
            )
            assert queued_resp.status_code == 200
            assert "Queued for processing" in queued_resp.text, \
                "5: a .txt upload should report queued immediately"

            needs_review_row = _wait_for_needs_review(db_path, "aassist")
            assert needs_review_row is not None, \
                "5: no needs_review audit row appeared for aassist within the deadline"
        finally:
            upload_worker._extract = orig_extract

        # 6. SC3 denial - admin is denied server-side and an allowed=0 audit
        # row is written; no file is ever sorted
        admin_dashboard_resp = admin_client.get("/")
        csrf6 = _csrf_from(admin_dashboard_resp.text)
        denial_resp = admin_client.post(
            "/upload",
            data={"csrf_token": csrf6, "files": (io.BytesIO(b"\xff\xd8\xff"), "sneak.png")},
            content_type="multipart/form-data",
        )
        assert denial_resp.status_code == 302, "6: admin upload should redirect, never process files"
        assert not (sorted_root / "images" / "sneak.png").exists(), \
            "6: a denied admin upload must never reach the sorter"
        rows6 = _audit_rows(db_path, username="aadmin", action="upload_file", allowed=0)
        assert len(rows6) == 1, f"6: expected 1 denied audit row for aadmin, got {len(rows6)}"

        # 7. D-18/D-02 - the assistant's upload is accepted (write), but the
        # assistant's own patient-detail view still lists no filenames (no
        # read_clinical - the write-only-no-read invariant)
        assistant_detail_resp = assistant_client.get(f"/patients/{VALID_CF}")
        csrf7 = _csrf_from(assistant_detail_resp.text)
        assistant_upload_resp = assistant_client.post(
            f"/patients/{VALID_CF}/upload",
            data={"csrf_token": csrf7, "files": (io.BytesIO(b"\xff\xd8\xff"), "molar.jpg")},
            content_type="multipart/form-data",
            headers={"HX-Request": "true"},
        )
        assert assistant_upload_resp.status_code == 200
        rows7 = _audit_rows(db_path, username="aassist", action="upload_file", allowed=1)
        assert any("molar.jpg" in (row["target"] or "") for row in rows7), \
            "7: assistant's media upload should be accepted with an allowed=1 audit row"

        assistant_detail_resp2 = assistant_client.get(f"/patients/{VALID_CF}")
        assert assistant_detail_resp2.status_code == 200
        assert "molar.jpg" not in assistant_detail_resp2.text, \
            "7: assistant detail view must never list an uploaded filename (D-02/D-18)"

        # 8. D-19 - recent-intake is per-user, no cross-user leak
        assistant_recent_resp = assistant_client.get("/upload/recent")
        assert assistant_recent_resp.status_code == 200
        assert "molar.jpg" in assistant_recent_resp.text, \
            "8: assistant should see their own recent upload"
        assert "invoice" not in assistant_recent_resp.text and "xray.pdf" not in assistant_recent_resp.text, \
            "8: assistant recent-intake must not leak the dentist's uploads"

        dentist_recent_resp = dentist_client.get("/upload/recent")
        assert dentist_recent_resp.status_code == 200
        assert "molar.jpg" not in dentist_recent_resp.text, \
            "8: dentist recent-intake must not leak the assistant's upload"
        assert "invoice" in dentist_recent_resp.text, \
            "8: dentist should see their own recent uploads"

        # 9. friendly 413 banner (BLOCKER-2) - a followed redirect renders the
        # alert-danger banner, never Werkzeug's raw error stub
        old_max = app.config["MAX_CONTENT_LENGTH"]
        app.config["MAX_CONTENT_LENGTH"] = 10
        try:
            big_resp = dentist_client.post(
                "/upload",
                data={"csrf_token": "irrelevant", "files": (io.BytesIO(b"0123456789ABCDEF"), "big.png")},
                content_type="multipart/form-data",
                follow_redirects=True,
            )
        finally:
            app.config["MAX_CONTENT_LENGTH"] = old_max
        assert big_resp.status_code == 200, "9: the followed redirect should render 200"
        assert "exceed the 25MB limit" in big_resp.text, "9: friendly 413 banner text is missing"
        assert "alert-danger" in big_resp.text, "9: the banner should render inside alert-danger"
        assert "Redirecting..." not in big_resp.text, \
            "9: must never show Werkzeug's raw redirect/413 stub"

        # 10. flash-category regression guard (BLOCKER-3) - a categoryless
        # denial flash must map to alert-primary, never the broken alert-message
        admin_patients_resp = admin_client.get(f"/patients/{VALID_CF}", follow_redirects=True)
        assert admin_patients_resp.status_code == 200
        assert "alert-primary" in admin_patients_resp.text, \
            "10: a categoryless flash should render as alert-primary"
        assert "alert-message" not in admin_patients_resp.text, \
            "10: must never render the broken alert-message class"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python upload_routes_selftest.py --selftest")


if __name__ == "__main__":
    main()
