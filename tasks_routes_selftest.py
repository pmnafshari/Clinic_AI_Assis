"""P29 NEW-04: tasks for the staff. A dentist or reception adds one - on the Tasks page or on a patient's care plan -
for one member of staff with a due time; overdue ones are flagged on the page, on Home, in the bell and in the menu's
count of one's own; done or cancelled once; admin has none of it; every change is audited."""
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

from werkzeug.security import generate_password_hash

import clinic_time
import patient_id
import storage
import web_session

CF = "ZZTK800101010101"


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        import app.db as app_db
        from app import create_app
        db = str(Path(tmp) / "tasks.sqlite")
        app_db.DB_PATH = db
        app_db.CHROMA_PATH = str(Path(tmp) / "chroma")
        conn = storage.init_db(db)
        for user, role in (("dr_task", "dentist"), ("desk", "assistant"), ("boss", "admin")):
            conn.execute("INSERT INTO users (username, password_hash, role, active) VALUES (?, ?, ?, 1)",
                         (user, generate_password_hash("x"), role))
        pid = patient_id.seed_patient(conn, CF, "Tina Compito", "+393400000041")
        conn.commit()
        app = create_app()
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False

        def client(user, role):
            c = app.test_client()
            c.set_cookie(web_session.COOKIE_NAME, web_session.create_session(conn, user, role))
            return c
        dentist, desk, boss = client("dr_task", "dentist"), client("desk", "assistant"), client("boss", "admin")
        now = clinic_time.now()
        past, later = now - timedelta(hours=2), now + timedelta(days=3)

        # 1. the page: dentist and reception, with the menu entry; admin is turned away
        page = desk.get("/tasks").get_data(as_text=True)
        assert 'action="/tasks"' in page and 'value="dr_task"' in page and 'value="boss"' not in page, \
            "1: the new-task form, or admin offered as an assignee"
        assert 'href="/tasks"' in page, "1: no Tasks in the menu"
        assert boss.get("/tasks").status_code == 302 and 'href="/tasks"' not in boss.get("/").get_data(as_text=True), \
            "1: admin reaches the tasks"

        # 2. add: from the page (overdue, for reception) and from the record (later, for the dentist, about the patient)
        dentist.post("/tasks", data={"title": "Call the lab about the crown", "assignee": "desk",
                                     "day": past.date().isoformat(), "time": past.strftime("%H:%M")})
        resp = desk.post("/tasks", data={"title": "Check the x-ray", "assignee": "dr_task", "cf": CF,
                                         "day": later.date().isoformat(), "time": "10:00"})
        assert resp.headers["Location"].endswith(f"/patients/{CF}#tasks"), "2: not back on the record"
        bad = desk.post("/tasks", data={"title": "x", "assignee": "boss", "day": later.date().isoformat()},
                        follow_redirects=True).get_data(as_text=True)
        assert "Choose who does it." in bad, "2: an admin assignee is not refused with a reason"
        boss.post("/tasks", data={"title": "sneaky", "assignee": "desk", "day": later.date().isoformat()})
        rows = conn.execute("SELECT title, assignee, patient_id, created_by FROM staff_tasks ORDER BY id").fetchall()
        assert [tuple(r) for r in rows] == [("Call the lab about the crown", "desk", None, "dr_task"),
                                            ("Check the x-ray", "dr_task", pid, "desk")], f"2: {[tuple(r) for r in rows]}"

        # 3. overdue: flagged on the page, counted on Home, in the bell and the menu - for its own person only
        page = desk.get("/tasks").get_data(as_text=True)
        mine = page.split('id="mine"', 1)[1].split('id="other-tasks-title"', 1)[0]
        assert "Call the lab about the crown" in mine and "Overdue" in mine, "3: the overdue task is not flagged as mine"
        home = desk.get("/").get_data(as_text=True)
        assert "1 of your tasks is overdue" in home and "/tasks#mine" in home, "3: Home does not say it"
        assert "1 task due" in home, "3: the bell does not count it"
        assert "of your tasks is overdue" not in dentist.get("/").get_data(as_text=True), "3: another's task counted"
        record = desk.get(f"/patients/{CF}").get_data(as_text=True)
        assert 'id="tasks"' in record and "Check the x-ray" in record and "Call the lab" not in record.split('id="tasks"')[1], \
            "3: the record's tasks are not this patient's"

        # 4. done and cancelled, once; audited; admin cannot
        t1, t2 = [r[0] for r in conn.execute("SELECT id FROM staff_tasks ORDER BY id")]
        boss.post(f"/tasks/{t1}/close", data={"status": "done"})
        desk.post(f"/tasks/{t1}/close", data={"status": "done"})
        assert desk.post(f"/tasks/{t1}/close", data={"status": "cancelled"}).status_code == 404, "4: closed twice"
        dentist.post(f"/tasks/{t2}/close", data={"status": "cancelled", "cf": CF})
        assert [tuple(r) for r in conn.execute("SELECT status, closed_by FROM staff_tasks ORDER BY id")] == \
            [("done", "desk"), ("cancelled", "dr_task")], "4: done and cancelled"
        assert "of your tasks is overdue" not in desk.get("/").get_data(as_text=True), "4: a done task still counted"
        n = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action LIKE 'task_%' AND allowed = 1").fetchone()[0]
        assert n == 4, f"4: the task changes are not audited ({n})"
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action LIKE 'task_%' AND allowed = 0").fetchone()[0] == 2
        closed = desk.get("/tasks").get_data(as_text=True).split('id="closed-tasks-title"', 1)[1]
        assert "Call the lab about the crown" in closed and "Check the x-ray" in closed, "4: the closed list"

        # 5. in Italian
        desk.set_cookie("staff_lang", "it")
        assert "Compiti" in desk.get("/tasks").get_data(as_text=True), "5: the tasks page is not in Italian"
        conn.close()
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        print("usage: python tasks_routes_selftest.py --selftest")
