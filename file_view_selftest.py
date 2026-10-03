"""Image files open inside the browser at a useful size (DEMO-OPG): the dentist's Files page and record view, and
the patient's "My files" - fit to the screen, labelled, with a way to see the full image. Orientation only: no
viewer tools, no interpretation. PDFs keep their download-only behaviour.

Staff and portal apps over one temp database; nothing here needs a model or a server.
"""
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

import app.db as app_db
import documents as docs
import legacy_fixtures as fx
import legacy_import as li
import patient_files as pf
from patient_files_selftest import T0, confirmed, patient_client, png, staff_client

D = ("drossi", "dentist")


def tag(html, cls):
    m = re.search(r'<img[^>]*class="[^"]*\b' + cls + r'\b[^"]*"[^>]*>', html)
    return m.group(0) if m else ""


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db_path = str(tmp / "clinic.sqlite")
        app_db.DB_PATH = db_path
        app_db.CHROMA_PATH = str(tmp / "chroma")
        docs.DOC_ROOT = tmp / "documents"
        docs.DOC_CHROMA_PATH = str(tmp / "doc_chroma")
        docs.SLOT_DIR = tmp / "slots"
        docs.unindex = lambda ids: None
        li.STAGING_ROOT = tmp / "import_staging"
        from app import create_app
        from patient_app import create_patient_app, routes as proutes
        proutes.DB_PATH = db_path
        app = create_app()
        app.config["TESTING"] = True
        papp = create_patient_app(env_path=tmp / ".env.patient")
        papp.config["TESTING"] = True
        from storage import connect
        conn = connect(db_path)
        conn.row_factory = sqlite3.Row
        pids = fx.seed(conn)
        rossi, verdi = pids["rossi"], pids["verdi"]
        image = png(31)
        img_id = confirmed(conn, rossi, image, "DEMO-opg.png")
        conn.execute("UPDATE patient_documents SET category = 'opg' WHERE id = ?", (img_id,))
        pdf_id = confirmed(conn, rossi, fx.pdf(["Referto DEMO"]), "DEMO-referto.pdf")
        conn.commit()
        for did in (img_id, pdf_id):
            pf.publish(conn, did, rossi, *D, now=T0)
        cf = fx.CF["rossi"]
        dentist = staff_client(app, db_path, *D)
        p_rossi = patient_client(papp, db_path, cf)
        p_verdi = patient_client(papp, db_path, fx.CF["verdi"])
        preview = f"/patients/{cf}/documents/{img_id}/preview"

        # 1. the Files page: the image entry links to its view; the thumbnail is uncropped and labelled
        page = dentist.get(f"/patients/{cf}/documents").text
        assert f'href="/patients/{cf}/documents/{img_id}#preview"' in page, "1: the Files page links to the image view"
        thumb = tag(page, "file-thumb")
        assert thumb and 'alt="' in thumb and "for orientation only" in thumb, "1: the thumbnail has an accessible label"
        assert f"/patients/{cf}/documents/{pdf_id}#preview" not in page, "1: a PDF gets no image view"

        # 2. the record's file view: fitted to the screen, labelled, with the full image one click away
        page = dentist.get(f"/patients/{cf}/documents/{img_id}").text
        assert 'id="preview"' in page, "2: the view has an anchor the Files page can open"
        view = tag(page, "file-view")
        assert view and f'src="{preview}"' in view, "2: the image is shown inside the page"
        assert "DEMO-opg.png" in view and "for orientation only" in view, "2: the image is labelled with its name and use"
        full = re.search(r'<a[^>]*href="' + re.escape(preview) + r'"[^>]*>[^<]*Open full size', page)
        assert full and 'target="_blank"' in full.group(0) and 'rel="noopener"' in full.group(0), \
            "2: 'Open full size' opens the image itself in a new tab"
        css = (Path("app/static/css/app.css")).read_text()
        rule = re.search(r"\.file-view\s*\{([^}]*)\}", css)
        assert rule and "object-fit: contain" in rule.group(1) and "vh" in rule.group(1) and "max-width: 100%" in rule.group(1), \
            "2: the view fits the screen without cropping"
        thumb_rule = re.search(r"\.file-thumb\s*\{([^}]*)\}", css)
        assert thumb_rule and "object-fit: contain" in thumb_rule.group(1), "2: a panoramic thumbnail is not cropped"
        r = dentist.get(preview)
        assert r.status_code == 200 and r.data == image and r.mimetype == "image/png", "2: the preview is the stored bytes"
        page = dentist.get(f"/patients/{cf}/documents/{pdf_id}").text
        assert "Open full size" not in page and "file-view" not in page, "2: a PDF stays download only"

        # 3. the patient's My files: the image itself at a useful size, labelled, full size one tap away
        page = p_rossi.get("/files").text
        shown = tag(page, "patient-file-image")
        portal_preview = f"/files/{img_id}/preview"
        assert shown and f'src="{portal_preview}"' in shown, "3: the image is shown inside My files"
        assert 'alt="' in shown and "OPG" in shown, "3: the image has an accessible label"
        assert "patient-file-thumb" not in page, "3: no cropped 96px square any more"
        full = re.search(r'<a[^>]*href="' + re.escape(portal_preview) + r'"[^>]*>', page)
        assert full and 'target="_blank"' in full.group(0) and 'rel="noopener"' in full.group(0), \
            "3: the full image opens in a new tab"
        assert "Apri a grandezza intera" in page or "Open full size" in page, "3: the full-size link says what it does"
        assert f"/files/{img_id}/download" in page and f"/files/{pdf_id}/download" in page, "3: downloads still offered"
        assert f"/files/{pdf_id}/preview" not in page, "3: a PDF gets no image"
        pcss = Path("patient_app/static/css/patient.css").read_text()
        rule = re.search(r"\.patient-file-image\s*\{([^}]*)\}", pcss)
        assert rule and "object-fit: contain" in rule.group(1) and "vh" in rule.group(1) and "width: 100%" in rule.group(1), \
            "3: the portal image fits the screen without cropping"
        r = p_rossi.get(portal_preview)
        assert r.status_code == 200 and r.data == image, "3: her preview is the stored bytes"

        # 4. another patient cannot open it by changing the address, in the portal or the page
        assert p_verdi.get(portal_preview).status_code == 404, "4: HARD FAIL - another patient opened the image"
        assert p_verdi.get(f"/files/{img_id}/download").status_code == 404, "4: HARD FAIL - another patient downloaded it"
        assert "patient-file-image" not in p_verdi.get("/files").text, "4: HARD FAIL - another patient's list shows it"
        assert dentist.get(f"/patients/{fx.CF['verdi']}/documents/{img_id}/preview").status_code == 404, \
            "4: the image is not reachable under another patient's record"
        conn.close()
    print("file_view_selftest: ok")


if __name__ == "__main__":
    selftest()
    sys.exit(0)
