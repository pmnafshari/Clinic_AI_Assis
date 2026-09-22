"""Human handoff queue (P10.05). A patient asks for a person; staff answer.

Nothing here messages anyone. There is no provider (D01, P11), so a handoff is
a row the clinic works through and a status the patient can see in the portal.
The portal never promises a response time - outside opening hours it says the
clinic is closed and when it opens next, and nothing more.

WHAT A ROW HOLDS, AND WHAT IT DOES NOT. The reason and the topic are closed
vocabularies; the patient's own words are never stored. A queue of what people
typed would rebuild the transcript phase 17 D-03 forbids, one row at a time -
the same rule the audit trail follows for `target`. Staff get the patient, the
topic and the time, which is what a call-back needs.
"""
import sqlite3
import sys

from datetime import datetime, timedelta

import clinic_time
from auth import authorize, log_audit

# why the conversation stopped being something software should answer
REASONS = (
    "symptom",          # the D-02 gate deflected a clinical symptom
    "treatment",        # ... or a request for medication or treatment
    "advice",           # ... or a request for an opinion
    "payment_unknown",  # the clinic has not recorded whether invoices were paid
    "no_content",       # asked something the approved content does not cover
    "patient_asked",    # asked for a person in as many words
)

# what it was about, at the coarsest level that is still useful to call back on
TOPICS = ("appointment", "billing", "clinical", "records", "other")

OPEN, CLAIMED, RESOLVED = "open", "claimed", "resolved"

SCHEMA = """
    CREATE TABLE IF NOT EXISTS handoff_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT NOT NULL,
        reason TEXT NOT NULL CHECK (reason IN
            ('symptom', 'treatment', 'advice', 'payment_unknown', 'no_content', 'patient_asked')),
        topic TEXT NOT NULL CHECK (topic IN
            ('appointment', 'billing', 'clinical', 'records', 'other')),
        status TEXT NOT NULL CHECK (status IN ('open', 'claimed', 'resolved')),
        created_at TEXT NOT NULL,
        claimed_by TEXT,
        claimed_at TEXT,
        resolved_by TEXT,
        resolved_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_handoff_status ON handoff_requests (status, created_at);
    CREATE INDEX IF NOT EXISTS idx_handoff_patient ON handoff_requests (patient_id);
    -- one open request per patient and topic: a patient who asks three times
    -- is one person waiting, not three, and a queue that says three is lying
    -- to whoever works it
    CREATE UNIQUE INDEX IF NOT EXISTS idx_handoff_one_open
        ON handoff_requests (patient_id, topic) WHERE status IN ('open', 'claimed');
"""


def raise_request(conn, pid, reason, topic, now=None):
    """Queue a handoff, or return the one already open. Returns (id, created)."""
    if reason not in REASONS:
        raise ValueError(f"unknown handoff reason {reason!r}")
    if topic not in TOPICS:
        raise ValueError(f"unknown handoff topic {topic!r}")
    now = now or clinic_time.now_utc()
    existing = conn.execute(
        "SELECT id FROM handoff_requests WHERE patient_id = ? AND topic = ?"
        " AND status IN ('open', 'claimed')", (pid, topic)).fetchone()
    if existing:
        return existing[0], False
    cur = conn.execute(
        "INSERT INTO handoff_requests (patient_id, reason, topic, status, created_at)"
        " VALUES (?, ?, ?, 'open', ?)", (pid, reason, topic, clinic_time.to_storage(now)))
    conn.commit()
    log_audit(conn, pid, "patient", "handoff_raised", f"{topic}:{reason}", allowed=1)
    return cur.lastrowid, True


def open_for_patient(conn, pid):
    return conn.execute(
        "SELECT * FROM handoff_requests WHERE patient_id = ? AND status IN ('open', 'claimed')"
        " ORDER BY created_at", (pid,)).fetchall()


def queue(conn, status=None):
    sql = ("SELECT h.*, p.patient_name FROM handoff_requests h LEFT JOIN patients p"
           " ON p.patient_id = h.patient_id")
    args = ()
    if status:
        sql += " WHERE h.status = ?"
        args = (status,)
    return conn.execute(sql + " ORDER BY h.status, h.created_at LIMIT 100", args).fetchall()


def counts(conn):
    got = {s: 0 for s in (OPEN, CLAIMED, RESOLVED)}
    for row in conn.execute("SELECT status, COUNT(*) FROM handoff_requests GROUP BY status"):
        got[row[0]] = row[1]
    return got


def claim(conn, handoff_id, actor, role, now=None):
    """Take ownership. One claimer wins; a second gets False, not an override."""
    if not authorize(role, "handle_handoff"):
        log_audit(conn, actor, role, "claim_handoff", f"handoff:{handoff_id}", allowed=0)
        raise PermissionError(f"{role} may not take a handoff")
    now = now or clinic_time.now_utc()
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        changed = conn.execute(
            "UPDATE handoff_requests SET status = 'claimed', claimed_by = ?, claimed_at = ?"
            " WHERE id = ? AND status = 'open'",
            (actor, clinic_time.to_storage(now), handoff_id)).rowcount
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    if changed:
        log_audit(conn, actor, role, "claim_handoff", f"handoff:{handoff_id}", allowed=1)
    return bool(changed)


def resolve(conn, handoff_id, actor, role, now=None):
    """Close it. Only the person holding it, or a dentist, may."""
    if not authorize(role, "handle_handoff"):
        log_audit(conn, actor, role, "resolve_handoff", f"handoff:{handoff_id}", allowed=0)
        raise PermissionError(f"{role} may not resolve a handoff")
    row = conn.execute("SELECT status, claimed_by FROM handoff_requests WHERE id = ?",
                       (handoff_id,)).fetchone()
    if row is None or row["status"] == RESOLVED:
        return False
    if row["status"] == CLAIMED and row["claimed_by"] != actor and role != "dentist":
        # reception cannot close what a colleague is holding; a dentist can
        log_audit(conn, actor, role, "resolve_handoff", f"handoff:{handoff_id}", allowed=0)
        raise PermissionError("that handoff is held by someone else")
    now = now or clinic_time.now_utc()
    conn.execute("UPDATE handoff_requests SET status = 'resolved', resolved_by = ?,"
                 " resolved_at = ? WHERE id = ?",
                 (actor, clinic_time.to_storage(now), handoff_id))
    conn.commit()
    log_audit(conn, actor, role, "resolve_handoff", f"handoff:{handoff_id}", allowed=1)
    return True


def next_opening(conn, now=None):
    """(open_now, next_opening_local) - never an ETA, only the clinic's hours.

    Returns the next moment the clinic is open, or None if the next seven days
    are all closed. The caller must not turn this into a promise about replies.
    """
    import availability
    now = now or clinic_time.now_utc()
    local = clinic_time.to_local(now)
    for ahead in range(8):
        day = local.date() + timedelta(days=ahead)
        row = availability.hours_for(conn, day.weekday())
        if row is None or row["closed"] or availability.is_closed_date(conn, day.isoformat()):
            continue
        opens = datetime.strptime(row["opens"], "%H:%M").time()
        closes = datetime.strptime(row["closes"], "%H:%M").time()
        start = datetime.combine(day, opens)
        if ahead == 0:
            if opens <= local.time() < closes:
                return True, None
            if local.time() >= closes:
                continue
        return False, start
    return False, None


def selftest():
    import tempfile
    import threading
    from pathlib import Path

    import availability
    import patient_id
    from storage import init_db

    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "clinic.sqlite")
        conn = init_db(db)
        D, A = ("drossi", "dentist"), ("aassist", "assistant")
        t0 = clinic_time.read_instant("2026-09-22T08:00:00+00:00")
        pid = patient_id.seed_patient(conn, "ZZH00A00A000A", "Hana Handoff", "+39 055 1")
        other = patient_id.seed_patient(conn, "ZZH00B00B000B", "Bruno Handoff", None)

        # 1. a handoff records why and what about - never what was typed
        hid, created = raise_request(conn, pid, "symptom", "clinical", now=t0)
        assert created and hid
        row = conn.execute("SELECT * FROM handoff_requests WHERE id = ?", (hid,)).fetchone()
        assert row["status"] == OPEN and row["reason"] == "symptom" and row["topic"] == "clinical"
        assert set(row.keys()) == {
            "id", "patient_id", "reason", "topic", "status", "created_at", "claimed_by",
            "claimed_at", "resolved_by", "resolved_at"}, \
            f"1: no column may hold free text: {set(row.keys())}"
        for bad in (("nonsense", "clinical"), ("symptom", "nonsense")):
            try:
                raise_request(conn, pid, *bad, now=t0)
                raise AssertionError(f"1: {bad} should be refused")
            except ValueError:
                pass

        # 2. asking three times is one person waiting, not three
        again, created_again = raise_request(conn, pid, "advice", "clinical", now=t0)
        assert again == hid and not created_again, "2: the open one is reused"
        assert len(open_for_patient(conn, pid)) == 1
        billing_id, created_billing = raise_request(conn, pid, "payment_unknown", "billing", now=t0)
        assert created_billing and billing_id != hid, "2: a different topic is its own request"

        # 3. one claimer wins, and the loser is told so rather than overriding
        assert claim(conn, hid, *A, now=t0) is True
        assert claim(conn, hid, "other_assist", "assistant", now=t0) is False, "3: no override"
        assert conn.execute("SELECT claimed_by FROM handoff_requests WHERE id = ?",
                            (hid,)).fetchone()[0] == "aassist"
        try:
            claim(conn, billing_id, "anadmin", "admin", now=t0)
            raise AssertionError("3: admin holds no handoff capability")
        except PermissionError:
            pass
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'claim_handoff'"
                            " AND allowed = 0").fetchone()[0] == 1, "3: the refusal is audited"

        # 4. reception cannot close a colleague's; a dentist can
        try:
            resolve(conn, hid, "other_assist", "assistant", now=t0)
            raise AssertionError("4: not the holder")
        except PermissionError:
            pass
        assert resolve(conn, hid, *D, now=t0) is True, "4: a dentist may"
        assert resolve(conn, hid, *D, now=t0) is False, "4: already resolved"
        assert counts(conn)[RESOLVED] == 1

        # 5. once resolved, the same topic can be raised again
        fresh, created_fresh = raise_request(conn, pid, "symptom", "clinical", now=t0)
        assert created_fresh and fresh != hid, "5: a resolved request does not block the next"

        # 6. two staff claiming the same row at the same moment: one wins
        conn.execute("UPDATE handoff_requests SET status = 'open', claimed_by = NULL"
                     " WHERE id = ?", (billing_id,))
        conn.commit()
        won, lock, gate = [], threading.Lock(), threading.Barrier(2)

        def grab(name):
            own = sqlite3.connect(db, timeout=10)
            own.row_factory = sqlite3.Row
            gate.wait()
            try:
                got = claim(own, billing_id, name, "assistant", now=t0)
            finally:
                own.close()
            with lock:
                won.append(got)

        threads = [threading.Thread(target=grab, args=(f"rush_{n}",)) for n in "ab"]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert won.count(True) == 1, f"6: exactly one claimer wins: {won}"

        # 7. the clinic's hours, never an ETA. open during hours, and outside
        # them the next real opening - not "we'll reply soon"
        availability.seed_fixture_hours(conn)
        open_now, nxt = next_opening(conn, clinic_time.to_utc(datetime(2026, 9, 22, 10, 0)))
        assert open_now is True and nxt is None, f"7: monday 10:00 is open: {open_now} {nxt}"
        open_now, nxt = next_opening(conn, clinic_time.to_utc(datetime(2026, 9, 22, 6, 0)))
        assert open_now is False and nxt == datetime(2026, 9, 22, 9, 0), f"7: before opening: {nxt}"
        open_now, nxt = next_opening(conn, clinic_time.to_utc(datetime(2026, 9, 22, 21, 0)))
        assert open_now is False and nxt == datetime(2026, 9, 23, 9, 0), f"7: after closing: {nxt}"
        assert nxt is None or isinstance(nxt, datetime), "7: a time, or nothing - never a promise"

        # 8. one patient's queue is not another's (erasure itself is asserted
        # in erasure.py's own selftest, against the real erasure path)
        assert conn.execute("SELECT COUNT(*) FROM handoff_requests WHERE patient_id = ?",
                            (other,)).fetchone()[0] == 0, "8: nothing leaked to another patient"
        assert all(r["patient_id"] == pid for r in open_for_patient(conn, pid))
        conn.close()
    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python handoff.py --selftest")
    sys.exit(1)


if __name__ == "__main__":
    main()
