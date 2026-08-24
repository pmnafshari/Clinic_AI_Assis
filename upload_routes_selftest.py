import io
import re
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

from werkzeug.security import generate_password_hash

import sort_files
from auth import log_audit
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
        # an admin no longer renders the dashboard - "/" lands them on their
        # own staff-accounts screen, so follow the redirect to get a token
        admin_dashboard_resp = admin_client.get("/", follow_redirects=True)
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

        # 11. SYNC-03 badge states (numbered 11, not the plan's 9/9b/9c/9d/
        # 9e - sections 9 and 10 above, the 413 banner and flash-category
        # regression guards, were added to this file after the plan's
        # <read_first> was written). each sub-case gets its own fresh user
        # so badge-presence assertions aren't muddied by earlier rows in the
        # same fragment.
        conn11 = sqlite3.connect(db_path)

        # 11 - synced .txt: upload_file allowed=1 + sync_note allowed=1 on
        # the same target collapses to one Sorted row
        _seed_user(db_path, "unote1", "goodpass", "assistant")
        client11 = _login(app, "unote1", "goodpass")
        target11 = f"sorted/{VALID_CF}/notes/synced11.txt"
        log_audit(conn11, "unote1", "assistant", "upload_file", target11, allowed=1)
        log_audit(conn11, "unote1", "assistant", "sync_note", target11, allowed=1)
        resp11 = client11.get("/upload/recent")
        assert resp11.status_code == 200
        assert "Sorted" in resp11.text, "11: a synced note should show Sorted"
        assert "Not searchable" not in resp11.text, "11: a synced note must not show Not searchable"
        assert resp11.text.count("synced11.txt") == 1, "11: synced note should appear exactly once"

        # 11b - failed sync: upload_file allowed=1 + sync_note allowed=0 on
        # the same target shows Not searchable, not Sorted (success criterion 4)
        _seed_user(db_path, "unote2", "goodpass", "assistant")
        client11b = _login(app, "unote2", "goodpass")
        target11b = f"sorted/{VALID_CF}/notes/failed11b.txt"
        log_audit(conn11, "unote2", "assistant", "upload_file", target11b, allowed=1)
        log_audit(conn11, "unote2", "assistant", "sync_note", target11b, allowed=0)
        resp11b = client11b.get("/upload/recent")
        assert resp11b.status_code == 200
        assert "Not searchable" in resp11b.text, "11b: a failed sync should show Not searchable"
        assert "Sorted" not in resp11b.text, "11b: a failed sync must not show Sorted for that file"
        assert resp11b.text.count("failed11b.txt") == 1, "11b: failed-sync note should appear exactly once"

        # 11c - media upload never gets a sync_note row, so it terminates at
        # Sorted and never shows Not searchable (D-09)
        _seed_user(db_path, "unote3", "goodpass", "assistant")
        client11c = _login(app, "unote3", "goodpass")
        target11c = f"sorted/{VALID_CF}/documents/xray11c.jpg"
        log_audit(conn11, "unote3", "assistant", "upload_file", target11c, allowed=1)
        resp11c = client11c.get("/upload/recent")
        assert resp11c.status_code == 200
        assert "Sorted" in resp11c.text, "11c: a media upload should show Sorted"
        assert "Not searchable" not in resp11c.text, "11c: a media upload must never show Not searchable"

        # 11d - a rejected upload still reads Rejected
        _seed_user(db_path, "unote4", "goodpass", "assistant")
        client11d = _login(app, "unote4", "goodpass")
        log_audit(conn11, "unote4", "assistant", "upload_file", "malware11d.dat", allowed=0)
        resp11d = client11d.get("/upload/recent")
        assert resp11d.status_code == 200
        assert "Rejected" in resp11d.text, "11d: a rejected upload should still show Rejected"

        # 11f - GUI-10. a note filed to needs_review is an AUTHORISED upload
        # (allowed=1), so before this phase it fell through to Sorted.
        _seed_user(db_path, "unote5", "goodpass", "assistant")
        client11f = _login(app, "unote5", "goodpass")
        log_audit(conn11, "unote5", "assistant", "upload_file",
                  "sorted/needs_review/broken11f.txt", allowed=1,
                  reason=sort_files.REASON_EXTRACT_FAILED)
        resp11f = client11f.get("/upload/recent")
        assert resp11f.status_code == 200
        assert "Needs Review" in resp11f.text, "11f: a needs_review note should show Needs Review"
        assert "Sorted" not in resp11f.text, "11f: a needs_review note must never show Sorted"
        assert sort_files.REASON_EXTRACT_FAILED in resp11f.text, \
            "11f: the badge should carry the stored reason as a tooltip"

        # 11g - an older row predating the reason column still reads honestly
        _seed_user(db_path, "unote6", "goodpass", "assistant")
        client11g = _login(app, "unote6", "goodpass")
        log_audit(conn11, "unote6", "assistant", "upload_file",
                  "sorted/needs_review/legacy11g.txt", allowed=1)
        resp11g = client11g.get("/upload/recent")
        assert "Needs Review" in resp11g.text, "11g: a reasonless needs_review row still shows Needs Review"
        assert "Sorted" not in resp11g.text, "11g: a reasonless needs_review row must never show Sorted"
        assert "open the file to see why" in resp11g.text, \
            "11g: a NULL reason should fall back to the generic tooltip, never an empty title"

        # 11h - the watcher won the race, so provenance is unknown. an honest
        # neutral beats a false success (D-10/D-12).
        _seed_user(db_path, "unote7", "goodpass", "assistant")
        client11h = _login(app, "unote7", "goodpass")
        log_audit(conn11, "unote7", "assistant", "upload_file",
                  "routed by watcher: raced11h.txt", allowed=1)
        resp11h = client11h.get("/upload/recent")
        assert "External" in resp11h.text, "11h: a watcher-raced row should show External"
        assert "Sorted" not in resp11h.text, "11h: a watcher-raced row must never show Sorted"

        # 11i - the needs_review test matches a path SEGMENT. a patient file
        # legitimately named needs_review.txt filed under a CF is Sorted.
        _seed_user(db_path, "unote8", "goodpass", "assistant")
        client11i = _login(app, "unote8", "goodpass")
        log_audit(conn11, "unote8", "assistant", "upload_file",
                  f"sorted/{VALID_CF}/notes/needs_review.txt", allowed=1)
        resp11i = client11i.get("/upload/recent")
        assert "Sorted" in resp11i.text, \
            "11i: a file merely NAMED needs_review.txt must not trip the segment test"
        assert "Needs Review" not in resp11i.text, "11i: filename must not false-positive as needs_review"

        # 11e - D-11/D-19 leak guard. a plain `username = ?` filter alone
        # would leak watcher rows to any account literally named "system",
        # so seed exactly that account and prove the role check is what
        # keeps its own upload separate from the watcher's row, not the
        # username match (which is identical for both rows here).
        _seed_user(db_path, "system", "goodpass", "assistant")
        system_named_client = _login(app, "system", "goodpass")
        own_target11e = f"sorted/{VALID_CF}/notes/mynote11e.txt"
        watcher_target11e = f"sorted/{VALID_CF}/notes/watcher11e.txt"
        log_audit(conn11, "system", "assistant", "upload_file", own_target11e, allowed=1)
        log_audit(conn11, "system", "system", "sync_note", watcher_target11e, allowed=1)
        conn11.close()

        resp11e = system_named_client.get("/upload/recent")
        assert resp11e.status_code == 200
        assert "mynote11e.txt" in resp11e.text, \
            "11e: the system-named account's own upload should still show"
        assert "watcher11e.txt" not in resp11e.text, \
            "11e: a role='system' row must never appear even when username matches (D-11)"
        assert "failed11b.txt" not in resp11e.text, \
            "11e: another user's sync_note row must never leak"

        # 12. two uploads of the same filename must not clobber each other in
        # drop/. the first extraction blocks so the second file is written
        # while the first is still sitting there - the actual race window.
        _seed_user(db_path, "udup", "goodpass", "assistant")
        dup_client = _login(app, "udup", "goodpass")
        release = threading.Event()

        def _blocking_unreachable(text):
            release.wait(5)
            raise OllamaUnreachable("offline")

        upload_worker._extract = _blocking_unreachable
        rows12 = []
        try:
            for body in (b"first note body", b"second note body"):
                dash12 = dup_client.get("/")
                dup_client.post(
                    "/upload",
                    data={
                        "csrf_token": _csrf_from(dash12.text),
                        "files": (io.BytesIO(body), "dup12.txt"),
                    },
                    content_type="multipart/form-data",
                    headers={"HX-Request": "true"},
                )
            assert len(list(drop_dir.glob("dup12*.txt"))) == 2, \
                f"12: same-named uploads overwrote each other in drop/, {list(drop_dir.iterdir())}"
            release.set()

            deadline = time.time() + 5.0
            while time.time() < deadline:
                rows12 = _audit_rows(db_path, username="udup", action="upload_file", allowed=1)
                if len(rows12) == 2:
                    break
                time.sleep(0.1)
        finally:
            release.set()
            upload_worker._extract = orig_extract

        assert len(rows12) == 2, f"12: expected 2 audit rows for udup, got {len(rows12)}"
        assert len({row["target"] for row in rows12}) == 2, \
            f"12: same-named uploads share one audit target: {[r['target'] for r in rows12]}"
        bodies = {p.read_bytes() for p in (sorted_root / "needs_review").glob("dup12*.txt")}
        assert bodies == {b"first note body", b"second note body"}, \
            f"12: a same-named upload was lost, found {bodies}"

        # 13. a queued .txt is recorded and shown as pending straight away, and
        # the routed row supersedes it in place instead of adding a second entry
        _seed_user(db_path, "uq13", "goodpass", "assistant")
        q_client = _login(app, "uq13", "goodpass")
        held = threading.Event()

        def _held_unreachable(text):
            held.wait(5)
            raise OllamaUnreachable("offline")

        upload_worker._extract = _held_unreachable
        try:
            dash13 = q_client.get("/")
            q_client.post(
                "/upload",
                data={
                    "csrf_token": _csrf_from(dash13.text),
                    "files": (io.BytesIO(b"note thirteen"), "note13.txt"),
                },
                content_type="multipart/form-data",
                headers={"HX-Request": "true"},
            )
            resp13 = q_client.get("/upload/recent")
            assert "Queued" in resp13.text, "13: a queued .txt should show as pending"
            assert resp13.text.count("note13.txt") == 1, \
                "13: a queued .txt should appear exactly once"

            held.set()
            deadline = time.time() + 5.0
            while time.time() < deadline:
                if _audit_rows(db_path, username="uq13", action="upload_file", allowed=1):
                    break
                time.sleep(0.1)
        finally:
            held.set()
            upload_worker._extract = orig_extract

        resp13b = q_client.get("/upload/recent")
        assert resp13b.text.count("note13.txt") == 1, \
            "13: the routed row should supersede the queued one, not sit beside it"
        assert "Queued" not in resp13b.text, "13: the pending badge should clear once the file is filed"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python upload_routes_selftest.py --selftest")


if __name__ == "__main__":
    main()
