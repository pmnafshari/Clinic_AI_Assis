"""P29 NEW-04: tasks for the staff, inside the app. A task names one staff member who does it, when it is due (clinic
time) and, if it is about a patient, which one. It is open until it is done or cancelled. One that is past its due
time is overdue; the home page, the bell and the menu count a person's own overdue and due-today tasks. Nothing is
sent to anyone - there is no messaging here.
"""
import sys
from datetime import datetime, timedelta

import clinic_time

SCHEMA = """
    CREATE TABLE IF NOT EXISTS staff_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        note TEXT,
        patient_id TEXT,
        assignee TEXT NOT NULL,
        due_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        closed_by TEXT,
        closed_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_staff_tasks_assignee ON staff_tasks (assignee, status);
    CREATE INDEX IF NOT EXISTS idx_staff_tasks_patient ON staff_tasks (patient_id);
"""
ROLES = ("dentist", "assistant")   # who a task can be given to


def staff(conn):
    """the people a task can be given to: active dentists and reception, by name"""
    return [r[0] for r in conn.execute("SELECT username FROM users WHERE active = 1 AND role IN (?, ?) ORDER BY username",
                                       ROLES)]


def create(conn, title, assignee, day, time, who, pid=None, note=None):
    """(id, None) or (None, why not). day and time are the clinic's own (YYYY-MM-DD, HH:MM)"""
    title = (title or "").strip()[:200]
    if not title:
        return None, "say what the task is"
    if assignee not in staff(conn):
        return None, "choose who does it"
    try:
        due = clinic_time.to_utc_text(datetime.strptime(f"{day} {time or '09:00'}", "%Y-%m-%d %H:%M"))
    except (TypeError, ValueError):
        return None, "the due date is not a date"
    if pid and conn.execute("SELECT 1 FROM patients WHERE patient_id = ?", (pid,)).fetchone() is None:
        return None, "no such patient"
    cur = conn.execute("INSERT INTO staff_tasks (title, note, patient_id, assignee, due_at, created_by, created_at)"
                       " VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (title, (note or "").strip()[:500] or None, pid or None, assignee, due, who, clinic_time.stamp()))
    conn.commit()
    return cur.lastrowid, None


def close(conn, tid, status, who):
    """done or cancelled, once. True when it was open"""
    if status not in ("done", "cancelled"):
        return False
    cur = conn.execute("UPDATE staff_tasks SET status = ?, closed_by = ?, closed_at = ? WHERE id = ? AND status = 'open'",
                       (status, who, clinic_time.stamp(), tid))
    conn.commit()
    return cur.rowcount == 1


def _view(row, now):
    t = dict(row)
    due = clinic_time.read_instant(t["due_at"])
    t["due_day"] = clinic_time.local_date(t["due_at"])
    t["due_time"] = clinic_time.local_hhmm(t["due_at"])
    t["overdue"] = t["status"] == "open" and due < now
    t["today"] = t["status"] == "open" and not t["overdue"] and t["due_day"] == clinic_time.to_local(now).date().isoformat()
    return t


def listing(conn, now=None, assignee=None, pid=None, status="open", days=30):
    """open ones by due time; done and cancelled ones from the last days, newest first"""
    now = now or clinic_time.now_utc()
    where, args = ["t.status = ?" if status == "open" else "t.status != 'open'"], [status] if status == "open" else []
    if status != "open":
        where.append("t.closed_at >= ?")
        args.append(clinic_time.to_storage(now - timedelta(days=days)))
    if assignee:
        where.append("t.assignee = ?")
        args.append(assignee)
    if pid:
        where.append("t.patient_id = ?")
        args.append(pid)
    order = "t.due_at, t.id" if status == "open" else "t.closed_at DESC, t.id DESC"
    rows = conn.execute("SELECT t.*, p.patient_name, p.codice_fiscale FROM staff_tasks t LEFT JOIN patients p"
                        f" ON p.patient_id = t.patient_id WHERE {' AND '.join(where)} ORDER BY {order}", args)
    return [_view(r, now) for r in rows]


def due_for(conn, username, now=None):
    """(overdue, due today) of this person's own open tasks"""
    mine = listing(conn, now, assignee=username)
    return sum(1 for t in mine if t["overdue"]), sum(1 for t in mine if t["today"])


def on_erase(conn, pid):
    """inside the erasure transaction: a task about the patient goes with them"""
    conn.execute("DELETE FROM staff_tasks WHERE patient_id = ?", (pid,))


def selftest():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE users (username TEXT, role TEXT, active INTEGER)")
    conn.executemany("INSERT INTO users VALUES (?, ?, ?)", [("dr_a", "dentist", 1), ("desk", "assistant", 1),
                                                           ("boss", "admin", 1), ("gone", "assistant", 0)])
    conn.execute("CREATE TABLE patients (patient_id TEXT PRIMARY KEY, patient_name TEXT, codice_fiscale TEXT)")
    conn.execute("INSERT INTO patients VALUES ('P1', 'Uno', 'CF1')")
    conn.executescript(SCHEMA)
    now = clinic_time.to_utc(datetime(2026, 11, 9, 12, 0))
    # 1. create: a title, a dentist or reception who is active, a real date; a patient only if there is one
    assert staff(conn) == ["desk", "dr_a"], "1: who a task can be given to"
    a, why = create(conn, "  Call the lab  ", "desk", "2026-11-09", "10:00", "dr_a", pid="P1")
    assert a and why is None, why
    for bad in ((" ", "desk", "2026-11-09", "10:00"), ("x", "boss", "2026-11-09", "10:00"), ("x", "gone", "2026-11-09", "10:00"),
                ("x", "desk", "9 Nov", "10:00"), ("x", "desk", "2026-11-09", "25:00")):
        assert create(conn, *bad, "dr_a")[0] is None, f"1: {bad} accepted"
    assert create(conn, "x", "desk", "2026-11-09", "10:00", "dr_a", pid="P9") == (None, "no such patient")
    b, _ = create(conn, "Order gloves", "desk", "2026-11-09", "18:00", "desk")
    c, _ = create(conn, "Check x-ray", "dr_a", "2026-11-10", "", "desk")
    # 2. overdue and today, from the clinic's clock; the person's own count
    rows = {t["id"]: t for t in listing(conn, now)}
    assert rows[a]["overdue"] and not rows[a]["today"], "2: 10:00 is past at 12:00"
    assert rows[b]["today"] and not rows[b]["overdue"], "2: 18:00 is later today"
    assert not rows[c]["today"] and not rows[c]["overdue"] and rows[c]["due_time"] == "09:00", "2: tomorrow, 09:00 by default"
    assert rows[a]["title"] == "Call the lab" and rows[a]["patient_name"] == "Uno", "2: title and patient"
    assert due_for(conn, "desk", now) == (1, 1) and due_for(conn, "dr_a", now) == (0, 0), "2: own counts"
    assert [t["id"] for t in listing(conn, now)] == [a, b, c], "2: by due time"
    # 3. done or cancelled, once; then in the closed list, not the open one
    assert close(conn, a, "done", "desk") and not close(conn, a, "cancelled", "desk") and not close(conn, b, "lost", "x")
    assert close(conn, b, "cancelled", "dr_a")
    assert [t["id"] for t in listing(conn, now)] == [c] and due_for(conn, "desk", now) == (0, 0), "3: closed ones"
    assert {t["id"] for t in listing(conn, clinic_time.now_utc(), status="closed")} == {a, b}, "3: the closed list"
    # 4. a patient's tasks go with the patient
    assert [t["id"] for t in listing(conn, now, pid="P1", status="closed")] == [a]
    on_erase(conn, "P1")
    assert conn.execute("SELECT COUNT(*) FROM staff_tasks WHERE patient_id = 'P1'").fetchone()[0] == 0, "4: erasure"
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        print("usage: python staff_tasks.py --selftest")
