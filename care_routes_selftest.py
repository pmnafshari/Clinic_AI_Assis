"""P29 E-002: the care plan on the patient record. A dentist and reception assign the services a patient should
attend, mark them done or take them off; admin sees none of it; every change is audited; the requests page lists the
ones waiting to be arranged; the patient's copy holds them."""
import sys
import tempfile
from pathlib import Path

from werkzeug.security import generate_password_hash

import patient_id
import storage
import web_session

CF, CF_OTHER = "ZZCP800101010101", "ZZCP800101010102"


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        import app.db as app_db
        from app import create_app
        db = str(Path(tmp) / "care.sqlite")
        app_db.DB_PATH = db
        app_db.CHROMA_PATH = str(Path(tmp) / "chroma")
        conn = storage.init_db(db)
        for user, role in (("dr_cura", "dentist"), ("desk", "assistant"), ("boss", "admin")):
            conn.execute("INSERT INTO users (username, password_hash, role, active) VALUES (?, ?, ?, 1)",
                         (user, generate_password_hash("x"), role))
        pid = patient_id.seed_patient(conn, CF, "Carla Piano", "+393400000031")
        other = patient_id.seed_patient(conn, CF_OTHER, "Oreste Altro", "+393400000032")
        conn.commit()
        app = create_app()
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False

        def client(user, role):
            c = app.test_client()
            c.set_cookie(web_session.COOKIE_NAME, web_session.create_session(conn, user, role))
            return c
        dentist, desk, boss = client("dr_cura", "dentist"), client("desk", "assistant"), client("boss", "admin")

        def rows(p):
            return sorted(tuple(r) for r in conn.execute(
                "SELECT service, status, assigned_by FROM patient_services WHERE patient_id = ?", (p,)))

        # 1. the Care plan tab: dentist and reception, never admin
        for who, name in ((dentist, "dentist"), (desk, "reception")):
            page = who.get(f"/patients/{CF}").get_data(as_text=True)
            assert 'id="tabbtn-care"' in page and 'id="services"' in page, f"1: {name} has no care plan"
            assert 'value="orthodontics"' in page, f"1: {name} is not offered the services"
        assert "tabbtn-care" not in boss.get(f"/patients/{CF}").get_data(as_text=True), "1: admin sees the care plan"

        # 2. assign: reception and the dentist; an unknown service is refused with a reason; admin cannot
        resp = desk.post(f"/patients/{CF}/services", data={"service": "hygiene", "note": "every six months"})
        assert resp.status_code == 302 and resp.headers["Location"].endswith("#services"), "2: not back on the plan"
        dentist.post(f"/patients/{CF}/services", data={"service": "orthodontics"})
        bad = dentist.post(f"/patients/{CF}/services", data={"service": "massage"}, follow_redirects=True)
        assert "Choose a service." in bad.get_data(as_text=True), "2: an unknown service is not explained"
        boss.post(f"/patients/{CF}/services", data={"service": "surgery"})
        assert rows(pid) == [("hygiene", "open", "desk"), ("orthodontics", "open", "dr_cura")], f"2: {rows(pid)}"
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'service_assign' AND allowed = 1")\
            .fetchone()[0] == 2, "2: the assignments are not audited"
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'service_assign' AND allowed = 0")\
            .fetchone()[0] == 1, "2: admin's refusal is not audited"
        page = desk.get(f"/patients/{CF}").get_data(as_text=True)
        assert "Oral hygiene" in page and "every six months" in page and "Waiting to be arranged" in page, \
            "2: the plan does not show the referral"

        # 3. the requests page lists what waits to be arranged, with the way to the record
        req = desk.get("/requests").get_data(as_text=True)
        assert 'data-kind="services"' in req and "Carla Piano" in req and f"/patients/{CF}#services" in req, \
            "3: the requests page does not list the referrals"

        # 4. done and removed; never through another patient's record
        hyg = conn.execute("SELECT id FROM patient_services WHERE service = 'hygiene'").fetchone()[0]
        assert desk.post(f"/patients/{CF_OTHER}/services/{hyg}/close", data={"status": "done"}).status_code == 404, \
            "4: HARD FAIL - closed through another patient's record"
        desk.post(f"/patients/{CF}/services/{hyg}/close", data={"status": "done"})
        orth = conn.execute("SELECT id FROM patient_services WHERE service = 'orthodontics'").fetchone()[0]
        dentist.post(f"/patients/{CF}/services/{orth}/close", data={"status": "removed"})
        assert rows(pid) == [("hygiene", "done", "desk"), ("orthodontics", "removed", "dr_cura")], f"4: {rows(pid)}"
        assert "Carla Piano" not in desk.get("/requests").get_data(as_text=True).split('data-kind="services"')[1], \
            "4: a closed referral still waits"
        assert rows(other) == [], "4: another patient touched"

        # 5. the patient's copy, and the page in Italian
        import data_rights
        assert [s["service"] for s in data_rights.patient_data(conn, pid, sections=["appointments"])["services"]] == \
            ["hygiene"], "5: the copy does not hold the services"
        desk.set_cookie("staff_lang", "it")
        assert "Piano di cura" in desk.get(f"/patients/{CF}").get_data(as_text=True), "5: the care plan is not in Italian"
        conn.close()
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        print("usage: python care_routes_selftest.py --selftest")
