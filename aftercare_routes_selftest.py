"""P29 R-020 / NEW-05: the dentist's aftercare page and the care plan's aftercare. The demo samples arrive as drafts,
marked so, and nothing is approved; a dentist writes a draft and approves it; reception and admin cannot; the care plan
shows reception and the dentist what the patient is shown for the treatments of the last 30 days."""
import json
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

from werkzeug.security import generate_password_hash

import clinic_time
import patient_id
import storage
import web_session

CF = "ZZAC800101010101"


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        import app.db as app_db
        from app import create_app
        db = str(Path(tmp) / "aftercare.sqlite")
        app_db.DB_PATH = db
        app_db.CHROMA_PATH = str(Path(tmp) / "chroma")
        conn = storage.init_db(db)
        for user, role in (("dr_after", "dentist"), ("desk", "assistant"), ("boss", "admin")):
            conn.execute("INSERT INTO users (username, password_hash, role, active) VALUES (?, ?, ?, 1)",
                         (user, generate_password_hash("x"), role))
        pid = patient_id.seed_patient(conn, CF, "Aldo Dopo", "+393400000051")
        day = (clinic_time.now() - timedelta(days=3)).date().isoformat()
        conn.execute("INSERT INTO visits (patient_id, visit_date, procedures, clinical_notes, source_path)"
                     " VALUES (?, ?, ?, 'ok', 'a.json')", (pid, day, json.dumps(["ext 48"])))
        conn.commit()
        app = create_app()
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False

        def client(user, role):
            c = app.test_client()
            c.set_cookie(web_session.COOKIE_NAME, web_session.create_session(conn, user, role))
            return c
        dentist, desk, boss = client("dr_after", "dentist"), client("desk", "assistant"), client("boss", "admin")

        # 1. the page: the dentist's, with every treatment's demo draft marked as such and nothing approved
        page = dentist.get("/aftercare").get_data(as_text=True)
        for topic in ("extraction", "filling", "rct", "crown", "implant", "cleaning", "perio"):
            assert f'id="topic-{topic}"' in page, f"1: no {topic}"
        assert page.count("demo sample, not clinical advice") == 14 and "data-approved" not in page, \
            "1: HARD FAIL - a demo sample is not marked, or something is approved"
        assert 'href="/aftercare"' in page, "1: no Aftercare in the dentist's menu"
        for who in (desk, boss):
            assert who.get("/aftercare").status_code == 302, "1: a non-dentist reaches the aftercare texts"

        # 2. the care plan says the patient sees nothing yet for the extraction
        record = desk.get(f"/patients/{CF}").get_data(as_text=True).split('id="aftercare"', 1)[1]
        assert 'data-aftercare-item="extraction"' in record and "Nothing approved" in record, "2: the plan's aftercare"

        # 3. a dentist writes a draft and approves it; reception and admin cannot
        dentist.post("/aftercare/extraction/it", data={"body": "Mordere la garza per mezz'ora."})
        desk.post("/aftercare/extraction/it", data={"body": "reception text"})
        assert dentist.post("/aftercare/whitening/it", data={"body": "x"}).status_code == 404, "3: an unknown topic"
        new = conn.execute("SELECT id, version, demo, created_by FROM aftercare_texts WHERE topic = 'extraction'"
                           " AND lang = 'it' AND status = 'draft'").fetchall()
        assert [tuple(r)[1:] for r in new] == [(2, 0, "dr_after")], f"3: {[tuple(r) for r in new]}"
        boss.post(f"/aftercare/texts/{new[0][0]}/approve")
        desk.post(f"/aftercare/texts/{new[0][0]}/approve")
        assert conn.execute("SELECT COUNT(*) FROM aftercare_texts WHERE status = 'approved'").fetchone()[0] == 0, \
            "3: HARD FAIL - a non-dentist approved aftercare"
        dentist.post(f"/aftercare/texts/{new[0][0]}/approve")
        assert dentist.post(f"/aftercare/texts/{new[0][0]}/approve").status_code == 404, "3: approved twice"
        assert conn.execute("SELECT approved_by FROM aftercare_texts WHERE status = 'approved'").fetchall()[0][0] == \
            "dr_after", "3: who approved it"
        n = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action LIKE 'aftercare_%' AND allowed = 1").fetchone()[0]
        assert n == 2, f"3: the draft and the approval are not audited ({n})"

        # 4. the care plan now shows the text the patient sees, with its version and who approved it
        record = desk.get(f"/patients/{CF}").get_data(as_text=True).split('id="aftercare"', 1)[1]
        assert "Mordere la garza" in record and "approved by dr_after" in record, "4: the plan does not show it"
        dentist.set_cookie("staff_lang", "it")
        assert "Cura dopo il trattamento" in dentist.get("/aftercare").get_data(as_text=True), "4: not in Italian"
        conn.close()
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        print("usage: python aftercare_routes_selftest.py --selftest")
