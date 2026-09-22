"""Reminders through the real routes (P09.04, T4): what the queue shows, that
sending is plainly off and why, who may retry, and that admin sees none of it."""
import re
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from werkzeug.security import generate_password_hash

import app.db as app_db
import clinic_time
import consent
import patient_id
import reminders
import web_session
from app import create_app


def _client(app, db_path, username, role):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR IGNORE INTO users (username, password_hash, role, active)"
                 " VALUES (?, ?, ?, 1)", (username, generate_password_hash("x"), role))
    conn.commit()
    token = web_session.create_session(conn, username, role)
    conn.close()
    client = app.test_client()
    client.set_cookie(web_session.COOKIE_NAME, token)
    return client


def _csrf(html):
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "clinic.sqlite")
        app_db.DB_PATH = db_path
        app_db.CHROMA_PATH = str(Path(tmp) / "chroma")
        app = create_app()
        app.config["TESTING"] = True
        dentist = _client(app, db_path, "rm_dentist", "dentist")
        reception = _client(app, db_path, "rm_assist", "assistant")

        # 1. an empty queue says so, and never shows a schedule as active
        page = dentist.get("/reminders").text
        assert "schedule: not configured" in page and "Last run never" in page, \
            "1: an unscheduled job is never shown as active"
        assert "Sending is off" in page and "no messaging provider" in page, \
            "1: the page says nothing is sent, and why"
        assert "Nothing is queued." in page, "1: an empty queue says so"

        # 2. a booked appointment becomes one waiting reminder
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        t0 = clinic_time.read_instant("2026-09-22T08:00:00+00:00")
        pid = patient_id.seed_patient(conn, "ZZR00A00A000A", "Rina Reminder", "+39 055 000001")
        consent.record(conn, pid, "messaging", True, "rm_dentist", "dentist")
        stored = clinic_time.to_storage(clinic_time.to_utc(datetime(2026, 9, 24, 10, 0)))
        conn.execute("INSERT INTO appointments (patient_id, dentist, starts_at, minutes, status,"
                     " created_at, updated_at) VALUES (?, 'rm_dentist', ?, 30, 'booked', ?, ?)",
                     (pid, stored, stored, stored))
        conn.commit()
        assert reminders.plan(conn, now=t0) == 1
        page = reception.get("/reminders").text
        assert "Rina Reminder · appointment" in page and "Nothing is queued." not in page, \
            "2: reception sees the queue"
        assert "2026-09-23" in page, "2: due the day before, in the staff screens' date format"

        # 3. the counts filter the list, and delivered is shown as never set
        assert "no channel reports delivery yet" in page, "3: delivered is explained, not hidden"
        assert "Nothing in that state." in reception.get("/reminders?status=sent").text, \
            "3: filtering by a state with nothing in it says so"
        assert "Rina Reminder" in reception.get("/reminders?status=scheduled").text

        # 4. only a failed reminder offers a retry, and it only requeues
        job_id = conn.execute("SELECT id FROM reminder_jobs").fetchone()[0]
        assert "Try again" not in page, "4: a waiting reminder is not retryable"
        conn.execute("UPDATE reminder_jobs SET status = 'failed', attempts = 3,"
                     " last_error = 'transport_timeout' WHERE id = ?", (job_id,))
        conn.commit()
        page = reception.get("/reminders").text
        assert "Try again" in page and "transport timeout" in page, "4: a failed one explains itself"
        reception.post(f"/reminders/{job_id}/retry", data={"csrf_token": _csrf(page)})
        row = conn.execute("SELECT status, attempts, manual_retries FROM reminder_jobs"
                           " WHERE id = ?", (job_id,)).fetchone()
        assert tuple(row) == ("scheduled", 0, 1), f"4: back in the queue, not sent: {tuple(row)}"
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'retry_reminder'"
                            " AND allowed = 1").fetchone()[0] == 1, "4: the retry is audited"

        # 5. admin sees no reminder at all - the queue names patients
        admin = _client(app, db_path, "rm_admin", "admin")
        assert admin.get("/reminders").status_code == 302, "5: admin has no reminders"
        assert "Reminders" not in admin.get("/", follow_redirects=True).text, \
            "5: and is not offered the link"
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'view_reminders'"
                            " AND allowed = 0").fetchone()[0] == 1, "5: the refusal is audited"
        conn.close()

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python reminders_routes_selftest.py --selftest")
    sys.exit(1)


if __name__ == "__main__":
    main()
