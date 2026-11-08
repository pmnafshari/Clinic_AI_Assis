"""P29 E-002: the services a patient should attend - oral hygiene, restorative, endodontics and the rest. A dentist or
reception assigns one, it stays open until it is marked done or taken off, and every change says who and when. It is
a referral for the desk to arrange, not a clinical decision: it books nothing and changes no record.
"""
import sys

import clinic_time

SCHEMA = """
    CREATE TABLE IF NOT EXISTS patient_services (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT NOT NULL,
        service TEXT NOT NULL,
        note TEXT,
        status TEXT NOT NULL DEFAULT 'open',
        assigned_by TEXT NOT NULL,
        assigned_role TEXT NOT NULL,
        assigned_at TEXT NOT NULL,
        closed_by TEXT,
        closed_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_patient_services_patient ON patient_services (patient_id);
"""

SERVICES = ("hygiene", "restorative", "endodontics", "surgery", "implants", "prosthetics", "orthodontics",
            "paediatric", "radiology")
STATUSES = ("open", "done", "removed")


def assign(conn, pid, service, who, role, note=None):
    """(id, None) or (None, why not). one open referral per service: asking again returns the open one"""
    if service not in SERVICES:
        return None, "choose a service"
    if conn.execute("SELECT 1 FROM patients WHERE patient_id = ?", (pid,)).fetchone() is None:
        return None, "no such patient"
    row = conn.execute("SELECT id FROM patient_services WHERE patient_id = ? AND service = ? AND status = 'open'",
                       (pid, service)).fetchone()
    if row:
        return row[0], None
    cur = conn.execute("INSERT INTO patient_services (patient_id, service, note, assigned_by, assigned_role, assigned_at)"
                       " VALUES (?, ?, ?, ?, ?, ?)",
                       (pid, service, (note or "").strip()[:200] or None, who, role, clinic_time.stamp()))
    conn.commit()
    return cur.lastrowid, None


def close(conn, pid, sid, status, who):
    """mark an open referral done, or take it off. True when it was open and this patient's"""
    if status not in ("done", "removed"):
        return False
    cur = conn.execute("UPDATE patient_services SET status = ?, closed_by = ?, closed_at = ? WHERE id = ?"
                       " AND patient_id = ? AND status = 'open'", (status, who, clinic_time.stamp(), sid, pid))
    conn.commit()
    return cur.rowcount == 1


def for_patient(conn, pid):
    """open ones first, then the rest newest first"""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM patient_services WHERE patient_id = ? AND status != 'removed'"
        " ORDER BY status = 'open' DESC, assigned_at DESC, id DESC", (pid,))]


def waiting(conn):
    """every open referral, oldest first, with the patient it is for - the desk's list to arrange"""
    return [dict(r) for r in conn.execute(
        "SELECT s.*, p.patient_name, p.codice_fiscale FROM patient_services s JOIN patients p"
        " ON p.patient_id = s.patient_id WHERE s.status = 'open' ORDER BY s.assigned_at, s.id")]


def exportable(conn, pid):
    return [{"service": r["service"], "status": r["status"], "assigned_at": r["assigned_at"], "note": r["note"]}
            for r in for_patient(conn, pid)]


def on_erase(conn, pid):
    """inside the erasure transaction"""
    conn.execute("DELETE FROM patient_services WHERE patient_id = ?", (pid,))


def selftest():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE patients (patient_id TEXT PRIMARY KEY, patient_name TEXT, codice_fiscale TEXT)")
    conn.executemany("INSERT INTO patients VALUES (?, ?, ?)", [("P1", "Uno", "CF1"), ("P2", "Due", "CF2")])
    conn.executescript(SCHEMA)
    # 1. assign: a known service, a real patient; the same one again is the open one, not a second
    a, why = assign(conn, "P1", "hygiene", "desk", "assistant", "  every 6 months  ")
    assert a and why is None, why
    assert assign(conn, "P1", "hygiene", "dr", "dentist")[0] == a, "1: a second open hygiene referral"
    assert assign(conn, "P1", "massage", "dr", "dentist") == (None, "choose a service"), "1: an unknown service"
    assert assign(conn, "P9", "hygiene", "dr", "dentist") == (None, "no such patient"), "1: no patient"
    b, _ = assign(conn, "P1", "orthodontics", "dr", "dentist")
    assert [r["service"] for r in for_patient(conn, "P1")] == ["orthodontics", "hygiene"], "1: newest open first"
    assert for_patient(conn, "P1")[1]["note"] == "every 6 months", "1: the note"
    # 2. close: done or removed, only this patient's, only once
    assert not close(conn, "P2", a, "done", "desk"), "2: HARD FAIL - closed through another patient"
    assert not close(conn, "P1", a, "lost", "desk"), "2: an unknown status"
    assert close(conn, "P1", a, "done", "desk") and not close(conn, "P1", a, "removed", "desk"), "2: once"
    assert close(conn, "P1", b, "removed", "dr")
    assert [(r["service"], r["status"]) for r in for_patient(conn, "P1")] == [("hygiene", "done")], "2: removed is gone"
    assert assign(conn, "P1", "hygiene", "dr", "dentist")[0] != a, "2: done, so a new referral can open"
    # 3. the desk's list: every open one with its patient; the copy; erasure
    c, _ = assign(conn, "P2", "radiology", "dr", "dentist")
    assert [(r["patient_name"], r["service"]) for r in waiting(conn)] == [("Uno", "hygiene"), ("Due", "radiology")]
    assert {r["status"] for r in exportable(conn, "P1")} == {"open", "done"}, "3: the copy"
    on_erase(conn, "P1")
    assert for_patient(conn, "P1") == [] and len(waiting(conn)) == 1 and c, "3: erasure"
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        print("usage: python patient_services.py --selftest")
