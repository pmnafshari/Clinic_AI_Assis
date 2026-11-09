"""P29 NEW-06: the win-back page. Reception and the dentist see the patients not seen for 12 months who agreed to be
contacted, and record a contact; admin has none of it; a patient without the consent is never listed and cannot be
recorded as contacted; every contact is audited."""
import sys
import tempfile
from pathlib import Path

from werkzeug.security import generate_password_hash

import consent
import patient_id
import storage
import web_session


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        import app.db as app_db
        from app import create_app
        db = str(Path(tmp) / "outreach.sqlite")
        app_db.DB_PATH = db
        app_db.CHROMA_PATH = str(Path(tmp) / "chroma")
        conn = storage.init_db(db)
        for user, role in (("dr_win", "dentist"), ("desk", "assistant"), ("boss", "admin")):
            conn.execute("INSERT INTO users (username, password_hash, role, active) VALUES (?, ?, ?, 1)",
                         (user, generate_password_hash("x"), role))
        yes = patient_id.seed_patient(conn, "ZZWB000000000001", "Wanda Ritorno", "3331112222")
        no = patient_id.seed_patient(conn, "ZZWB000000000002", "Nora Riservata", "3331113333")
        for pid in (yes, no):
            conn.execute("INSERT INTO visits (patient_id, visit_date, procedures, clinical_notes, source_path)"
                         " VALUES (?, '2024-05-01', '[]', '', ?)", (pid, f"{pid}.json"))
        consent.record(conn, yes, "marketing", True, "desk", "assistant")
        conn.commit()
        app = create_app()
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False

        def client(user, role):
            c = app.test_client()
            c.set_cookie(web_session.COOKIE_NAME, web_session.create_session(conn, user, role))
            return c
        dentist, desk, boss = client("dr_win", "dentist"), client("desk", "assistant"), client("boss", "admin")
        # 1. the list: the consenting patient only; reception and dentist; admin turned away
        for who in (desk, dentist):
            page = who.get("/outreach").get_data(as_text=True)
            assert "Wanda Ritorno" in page and "Nora Riservata" not in page, "1: HARD FAIL - the list ignores consent"
            assert 'href="/outreach"' in page, "1: no Win back in the menu"
        assert boss.get("/outreach").status_code == 302, "1: admin reaches the list"
        # 2. a contact: recorded and audited; refused for a patient without the consent, or with no outcome
        desk.post("/outreach/ZZWB000000000001/contact", data={"outcome": "no_answer", "note": "evening"})
        desk.post("/outreach/ZZWB000000000002/contact", data={"outcome": "reached"})
        dentist.post("/outreach/ZZWB000000000001/contact", data={"outcome": ""})
        boss.post("/outreach/ZZWB000000000001/contact", data={"outcome": "reached"})
        rows = [tuple(r) for r in conn.execute("SELECT patient_id, outcome, note, by_user FROM outreach_contacts")]
        assert rows == [(yes, "no_answer", "evening", "desk")], f"2: {rows}"
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'outreach_contact' AND allowed = 1")\
            .fetchone()[0] == 1, "2: not audited"
        page = desk.get("/outreach").get_data(as_text=True)
        assert "No answer" in page and "evening" in page, "2: the last contact is not shown"
        # 3. a withdrawn consent takes the patient off at once; in Italian
        consent.record(conn, yes, "marketing", False, yes, "patient")
        assert "Wanda Ritorno" not in desk.get("/outreach").get_data(as_text=True), "3: HARD FAIL - withdrawn but listed"
        desk.set_cookie("staff_lang", "it")
        assert "Pazienti da ricontattare" in desk.get("/outreach").get_data(as_text=True), "3: not in Italian"
        conn.close()
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        print("usage: python outreach_routes_selftest.py --selftest")
