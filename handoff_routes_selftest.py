"""The call-back queue through the real routes (P10.05, T4): who may work it,
the claim race on screen, what the row shows and what it refuses to show."""
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

from werkzeug.security import generate_password_hash

import app.db as app_db
import availability
import clinic_time
import handoff
import patient_id
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
        dentist = _client(app, db_path, "hf_dentist", "dentist")
        reception = _client(app, db_path, "hf_assist", "assistant")
        other = _client(app, db_path, "hf_assist2", "assistant")

        # 1. an empty queue says so, and never promises a reply time
        page = dentist.get("/handoffs").text
        assert "Nobody is waiting." in page, "1: an empty queue says so"
        assert "Nothing was sent to the patient" in page, "1: it says nothing went out"
        # word-boundary, not substring: "eta" lives inside "metadata" and
        # "details", and a check that fires on those tells you nothing
        for promise in (r"we will reply", r"we'll reply", r"within \d", r"response time",
                        r"eta", r"soon"):
            assert not re.search(rf"\b{promise}\b", page.lower()), \
                f"1: the page promises {promise!r}"

        # 2. a raised call-back carries the patient, the topic and the reason -
        # and no column that could hold what the patient typed
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        availability.seed_fixture_hours(conn)
        pid = patient_id.seed_patient(conn, "ZZH00C00C000C", "Carla Chiamata", "+39 055 1")
        t0 = clinic_time.read_instant("2026-09-22T08:00:00+00:00")
        hid, _ = handoff.raise_request(conn, pid, "symptom", "clinical", now=t0)
        page = reception.get("/handoffs").text
        assert "Carla Chiamata · clinical" in page and "symptom" in page, "2: reception sees it"
        assert "Take it" in page, "2: and can take it"

        # 3. one claimer wins on screen; the loser is told, not overridden
        reception.post(f"/handoffs/{hid}/claim", data={"csrf_token": _csrf(page)})
        page2 = other.get("/handoffs").text
        assert "with hf_assist" in page2, "3: the holder is named"
        other.post(f"/handoffs/{hid}/claim", data={"csrf_token": _csrf(page2)})
        assert conn.execute("SELECT claimed_by FROM handoff_requests WHERE id = ?",
                            (hid,)).fetchone()[0] == "hf_assist", "3: no override"

        # 4. a colleague cannot close what someone else holds; a dentist can
        page2 = other.get("/handoffs").text
        other.post(f"/handoffs/{hid}/resolve", data={"csrf_token": _csrf(page2)})
        assert conn.execute("SELECT status FROM handoff_requests WHERE id = ?",
                            (hid,)).fetchone()[0] == "claimed", "4: still held"
        page = dentist.get("/handoffs").text
        dentist.post(f"/handoffs/{hid}/resolve", data={"csrf_token": _csrf(page)})
        assert conn.execute("SELECT status FROM handoff_requests WHERE id = ?",
                            (hid,)).fetchone()[0] == "resolved", "4: a dentist may"
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'resolve_handoff'"
                            " AND allowed = 1").fetchone()[0] == 1, "4: audited"

        # 5. admin sees no call-back at all - the queue names patients
        admin = _client(app, db_path, "hf_admin", "admin")
        assert admin.get("/handoffs").status_code == 302, "5: admin has no queue"
        assert "Call-backs" not in admin.get("/", follow_redirects=True).text, \
            "5: and is not offered the link"
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'view_handoffs'"
                            " AND allowed = 0").fetchone()[0] == 1, "5: the refusal is audited"

        # 6. the queue filters, and the clinic's hours are stated as hours
        assert "Nothing in that state." in reception.get("/handoffs?status=open").text
        page = reception.get("/handoffs").text
        assert ("Clinic open now" in page) or ("Clinic closed" in page), "6: hours, not an ETA"
        conn.close()

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python handoff_routes_selftest.py --selftest")
    sys.exit(1)


if __name__ == "__main__":
    main()
