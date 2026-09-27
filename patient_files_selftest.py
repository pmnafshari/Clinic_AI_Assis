"""Patient files (P25) through the real routes: the dentist's timeline, publication, correction,
the portal's "My files", and every way a file could reach the wrong person.

Two Flask apps (staff and patient) over one temp database. Nothing here needs a
model or a server. Image fixtures are hand-built PNG headers plus bytes; no
decoder runs.
"""
import re
import sqlite3
import struct
import sys
import tempfile
import zlib
from pathlib import Path

from werkzeug.security import generate_password_hash

import app.db as app_db
import clinic_time
import documents as docs
import legacy_fixtures as fx
import legacy_import as li
import patient_auth
import patient_files as pf
import web_session

D, A, ADM = ("drossi", "dentist"), ("aassist", "assistant"), ("anadmin", "admin")
T0 = clinic_time.read_instant("2026-09-27T08:00:00+00:00")


def png(seed):
    """A small valid PNG (1x1), distinct per seed."""
    raw = b"\x00" + bytes([seed % 256, 10, 20])
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def staff_client(app, db_path, username, role):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR IGNORE INTO users (username, password_hash, role, active)"
                 " VALUES (?, ?, ?, 1)", (username, generate_password_hash("x"), role))
    conn.commit()
    token = web_session.create_session(conn, username, role)
    conn.close()
    client = app.test_client()
    client.set_cookie(web_session.COOKIE_NAME, token)
    return client


def patient_client(papp, db_path, cf):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    patient_auth.issue_pin(cf, conn, "drossi")
    conn.execute("UPDATE patient_credentials SET must_change_pin = 0")
    conn.commit()
    token = patient_auth.create_patient_session(conn, cf)
    conn.close()
    client = papp.test_client()
    client.set_cookie(patient_auth.PATIENT_COOKIE_NAME, token)
    client.set_cookie("patient_lang", "en")
    return client


def csrf(html):
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def confirmed(conn, pid, data, name, now=T0):
    """A document through P15's own path: upload, then the dentist confirms it."""
    did = docs.ingest(conn, pid, data, name, *D, now=now)
    if docs.row(conn, did)["status"] == "pending_review":
        docs._index = lambda r: None
        docs.confirm(conn, did, pid, *D, now=now)
    return did


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db_path = str(tmp / "clinic.sqlite")
        app_db.DB_PATH = db_path
        app_db.CHROMA_PATH = str(tmp / "chroma")
        docs.DOC_ROOT = tmp / "documents"
        docs.DOC_CHROMA_PATH = str(tmp / "doc_chroma")
        docs.SLOT_DIR = tmp / "slots"
        li.STAGING_ROOT = tmp / "import_staging"
        docs.unindex = lambda ids: None
        from app import create_app
        from patient_app import create_patient_app, routes as proutes
        proutes.DB_PATH = db_path
        app = create_app()
        app.config["TESTING"] = True
        papp = create_patient_app(env_path=tmp / ".env.patient")
        papp.config["TESTING"] = True
        from storage import connect
        conn = connect(db_path)
        pids = fx.seed(conn)
        rossi, verdi = pids["rossi"], pids["verdi"]
        dentist = staff_client(app, db_path, *D)
        assistant = staff_client(app, db_path, *A)
        admin = staff_client(app, db_path, *ADM)
        p_rossi = patient_client(papp, db_path, fx.CF["rossi"])
        p_verdi = patient_client(papp, db_path, fx.CF["verdi"])

        # 1. empty states: the portal says there is nothing, the dentist's timeline too
        page = p_rossi.get("/files")
        assert page.status_code == 200 and "No files have been shared with you yet" in page.text, \
            "1: an empty portal file list says so"
        rec = dentist.get(f"/patients/{fx.CF['rossi']}/documents").text
        assert "No confirmed files yet" in rec, "1: an empty timeline says so"

        # 2. a confirmed file is on the dentist's timeline and nowhere on the portal
        visit = conn.execute("INSERT INTO visits (patient_id, visit_date, procedures, source_path)"
                             " VALUES (?, '2021-03-04', '[\"checkup\"]', 'fx/v1.json')", (rossi,)).lastrowid
        conn.commit()
        img = confirmed(conn, rossi, png(1), "opg-2021.png")
        conn.execute("UPDATE patient_documents SET category = 'opg', visit_id = ? WHERE id = ?", (visit, img))
        conn.commit()
        waiting = docs.ingest(conn, rossi, b"draft referral text", "draft.txt", *D)
        entries = pf.timeline(conn, rossi, *D)
        ids = [e["doc_id"] for e in entries if e["kind"] == "file"]
        assert img in ids and waiting not in ids, "2: only confirmed files are on the timeline"
        entry = next(e for e in entries if e.get("doc_id") == img)
        assert entry["visit_date"] == "2021-03-04" and entry["file_type"] == "PNG image" \
            and entry["category"] == "OPG" and "uploaded by drossi" in entry["source"], entry
        assert entry["date_basis"], "2: the timeline says what its date is"
        for who in (A, ADM):
            try:
                pf.timeline(conn, rossi, *who)
                raise AssertionError(f"2: {who[1]} read the file timeline")
            except PermissionError:
                pass
        rec = dentist.get(f"/patients/{fx.CF['rossi']}").text
        assert "File timeline" in rec and "opg-2021.png" in rec and "draft.txt" not in rec.split("File timeline")[1] \
            .split("</section>")[0], "2: the record page carries the confirmed timeline only"
        rec = assistant.get(f"/patients/{fx.CF['rossi']}").text
        assert "opg-2021" not in rec and "File timeline" not in rec, "2: the assistant's record shows no files"
        for path in ("/files", f"/files/{img}/download", f"/files/{img}/preview"):
            resp = p_rossi.get(path)
            assert "opg-2021" not in resp.text and resp.status_code in (200, 404), \
                f"2: HARD FAIL - an unpublished file appears on {path}"
        assert "No files have been shared with you yet" in p_rossi.get("/files").text, \
            "2: HARD FAIL - an unpublished file changes the portal (count or metadata)"

        # 3. publication is a separate dentist decision, through the route, with csrf
        page = dentist.get(f"/patients/{fx.CF['rossi']}/documents").text
        resp = dentist.post(f"/patients/{fx.CF['rossi']}/documents/{img}/publish",
                            data={"csrf_token": csrf(page)})
        assert resp.status_code == 302 and pf.published(conn, img, rossi), "3: the dentist publishes"
        page = assistant.get(f"/patients/{fx.CF['rossi']}/documents")
        resp = assistant.post(f"/patients/{fx.CF['rossi']}/documents/{img}/withdraw", data={})
        assert pf.published(conn, img, rossi), "3: the assistant cannot withdraw"
        try:
            pf.publish(conn, waiting, rossi, *D)
            raise AssertionError("3: a file waiting for review was published")
        except docs.DocumentError:
            pass
        pf.publish(conn, img, rossi, *D)
        assert conn.execute("SELECT COUNT(*) FROM document_publications WHERE document_id = ? AND"
                            " withdrawn_at IS NULL", (img,)).fetchone()[0] == 1, "3: publishing twice is one record"

        # 4. the patient sees it, downloads it with safe headers, previews it
        page = p_rossi.get("/files").text
        assert "OPG" in page and "Added" in page and "2026" in page, "4: the portal lists the file by category and the date it was added"
        assert "opg-2021.png" not in page, "4: the original file name never reaches the portal (it can name anyone)"
        assert "for orientation only" in page.lower(), "4: a preview is never presented as diagnostic"
        resp = p_rossi.get(f"/files/{img}/download")
        assert resp.status_code == 200 and resp.data == png(1), "4: the original, byte for byte"
        assert "opg-2021" not in resp.headers["Content-Disposition"], "4: a neutral download name"
        assert "attachment" in resp.headers["Content-Disposition"] and \
            resp.headers["X-Content-Type-Options"] == "nosniff" and "no-store" in resp.headers["Cache-Control"], \
            dict(resp.headers)
        resp = p_rossi.get(f"/files/{img}/preview")
        assert resp.status_code == 200 and resp.mimetype == "image/png" and "no-store" in resp.headers["Cache-Control"], "4: the preview is an image, never cached"
        assert "sandbox" in resp.headers["Content-Security-Policy"], "4: the preview runs nothing"

        # 5. direct URLs: another patient, another number, a withdrawn or corrected file
        none_body = p_verdi.get("/files/999999/download")
        for path in (f"/files/{img}/download", f"/files/{img}/preview"):
            other = p_verdi.get(path)
            assert other.status_code == 404 and other.data == none_body.data, \
                f"5: HARD FAIL - another patient's file answered differently on {path}"
        assert "No files have been shared with you yet" in p_verdi.get("/files").text, \
            "5: HARD FAIL - listed to another patient"
        assert p_rossi.get(f"/files/{waiting}/download").status_code == 404, "5: an unpublished file is not served"
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'patient_file_denied' AND"
                            " username = ?", (verdi,)).fetchone()[0] >= 2, "5: refused attempts are audited"
        # staff: a document number from another patient under this patient's address
        other_doc = confirmed(conn, verdi, png(2), "verdi.png")
        assert dentist.get(f"/patients/{fx.CF['rossi']}/documents/{other_doc}/preview").status_code == 404, "5: staff preview of another patient's document"
        assert dentist.get(f"/patients/{fx.CF['rossi']}/documents/{other_doc}/file").status_code == 404, "5: staff download of another patient's document"
        assert assistant.get(f"/patients/{fx.CF['rossi']}/documents/{img}/preview").status_code == 302, \
            "5: the assistant gets no preview"

        # 6. a changed original is never served, to anyone
        path = docs.original_path(docs.row(conn, img))
        keep = path.read_bytes()
        path.chmod(0o600)
        path.write_bytes(keep + b"x")
        assert p_rossi.get(f"/files/{img}/download").status_code == 404, "6: a changed original is refused"
        assert dentist.get(f"/patients/{fx.CF['rossi']}/documents/{img}/preview").status_code == 404, "6: a changed original is never previewed"
        assert dentist.get(f"/patients/{fx.CF['rossi']}/documents/{img}/file").status_code == 404, \
            "6: the staff download refuses a changed original too"
        path.write_bytes(keep)

        # 7. withdrawal revokes at once
        page = dentist.get(f"/patients/{fx.CF['rossi']}/documents").text
        dentist.post(f"/patients/{fx.CF['rossi']}/documents/{img}/withdraw",
                     data={"csrf_token": csrf(page), "reason": "wrong image"})
        assert not pf.published(conn, img, rossi), "7: withdrawn"
        assert p_rossi.get(f"/files/{img}/download").status_code == 404, "7: HARD FAIL - withdrawn but served"
        assert "No files have been shared with you yet" in p_rossi.get("/files").text, \
            "7: HARD FAIL - withdrawn but listed"

        # 8. correction: the file moves to the right patient, access follows at once
        pf.publish(conn, img, rossi, *D)
        try:
            pf.correct(conn, img, rossi, fx.CF["verdi"], "", *D)
            raise AssertionError("8: a correction without a reason")
        except docs.DocumentError:
            pass
        page = dentist.get(f"/patients/{fx.CF['rossi']}/documents").text
        dentist.post(f"/patients/{fx.CF['rossi']}/documents/{img}/correct",
                     data={"csrf_token": csrf(page), "to_cf": fx.CF["verdi"], "reason": "filed under the wrong record"})
        r = docs.row(conn, img)
        assert r["patient_id"] == verdi and r["visit_id"] is None, "8: moved, and the old visit link dropped"
        assert p_rossi.get(f"/files/{img}/download").status_code == 404, "8: HARD FAIL - the old patient still has it"
        assert p_verdi.get(f"/files/{img}/download").status_code == 404, \
            "8: HARD FAIL - a correction published the file to the new patient"
        events = pf.history(conn, img, *D)
        assert events[-1]["action"] == "corrected" and events[-1]["from_pid"] == rossi and events[-1]["reason"], "8: the correction is recorded with its reason"
        assert events[-2]["action"] == "withdrawn", "8: the correction withdrew the publication itself"
        assert img not in [e.get("doc_id") for e in pf.timeline(conn, rossi, *D)], "8: gone from the old patient's timeline"

        # 8b. a publication names its patient: a document moved by any path that forgot to withdraw
        # is served to nobody
        moved = confirmed(conn, rossi, png(3), "moved.png")
        pf.publish(conn, moved, rossi, *D)
        conn.execute("UPDATE patient_documents SET patient_id = ? WHERE id = ?", (verdi, moved))
        conn.commit()
        assert p_rossi.get(f"/files/{moved}/download").status_code == 404 and \
            p_verdi.get(f"/files/{moved}/download").status_code == 404, \
            "8b: HARD FAIL - a stale publication reached a patient"

        # 8c. no preview for what is not an image, anywhere; the download still works
        note = confirmed(conn, rossi, b"Referto: controllo periodico.", "controllo.txt")
        pf.publish(conn, note, rossi, *D)
        assert dentist.get(f"/patients/{fx.CF['rossi']}/documents/{note}/preview").status_code == 404, "8c: a text file has no staff preview"
        assert p_rossi.get(f"/files/{note}/preview").status_code == 404, "8c: a text file has no preview"
        assert p_rossi.get(f"/files/{note}/download").status_code == 200, "8c: the text download still works"

        # 9. export policy of P15 unchanged: confirmed originals, published or not; nothing staged
        arcs = [name for name, _b in docs.exportable(conn, verdi)]
        assert any(f"{img}-" in a for a in arcs) and any(f"{other_doc}-" in a for a in arcs), \
            "9: the export keeps every confirmed original, whatever the portal shows"
        # the export module itself is untouched by P25 (P15-D2 stands)

        # 10. imports through the routes: dentist queue, assistant counts only, admin nothing
        folder = tmp / "legacy"
        cases = fx.build(folder)
        fx.fill_clinic_ids(folder, pids)
        batch = li.stage(conn, folder, *A, now=T0)
        page = dentist.get("/imports").text
        assert "Proposed" in page and "Conflict" in page and "Unmatched" in page, "10: the dentist's queue"
        item = next(r for r in li.items(conn, batch) if r["rel_path"] == cases["E01"][0])
        page = dentist.get(f"/imports/items/{item['id']}").text
        assert fx.CF["rossi"] in page and "line" in page and "Mario Rossi" in page, "10: evidence with sources"
        assert "Nothing is attached until you confirm" in page, "10: the page says what confirming does"
        ap = assistant.get("/imports")
        assert ap.status_code == 200 and "Rossi" not in ap.text and "referti" not in ap.text \
            and fx.CF["rossi"] not in ap.text, "10: the assistant sees progress without names or paths"
        assert assistant.get(f"/imports/items/{item['id']}").status_code == 302, "10: no item for the assistant"
        assert assistant.get(f"/imports/items/{item['id']}/original").status_code == 302, "10: no original for the assistant"
        assert admin.get("/imports").status_code == 302, "10: admin has no import page"
        resp = dentist.post(f"/imports/items/{item['id']}/confirm",
                            data={"csrf_token": csrf(page), "patient_cf": fx.CF["rossi"],
                                  "expected_sha": item["sha256"], "reason": "", "visit_id": ""})
        assert resp.status_code == 302 and li.item(conn, item["id"])["state"] == "confirmed", "10: confirmed"
        new_doc = li.item(conn, item["id"])["document_id"]
        assert p_rossi.get(f"/files/{new_doc}/download").status_code == 404, \
            "10: HARD FAIL - an imported file reached the portal without publication"

        # 11. nav: the import page is linked for the roles that may open it, only
        assert "/imports" in dentist.get("/").text and "/imports" in assistant.get("/").text, "11: the import page is linked for dentist and assistant"
        assert "/imports" not in admin.get("/").text, "11: no import link for admin"
        assert "/files" in p_rossi.get("/").text, "11: the portal links My files"
        conn.close()
    print("patient_files_selftest: ok")


if __name__ == "__main__":
    selftest()
    sys.exit(0)
