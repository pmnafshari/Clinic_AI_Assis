"""support tickets (P21.01). a small log, not a helpdesk: open -> triaged -> closed with a
resolution, every step kept in an append-only history.

tickets are about the system, never about a patient: a summary that looks like a
codice fiscale, an e-mail address or a phone number is refused. alerts written by
health.py (and the X-ray demo) become tickets through from_alerts(), one per
alert line however often the log is read. nothing here sends anything: who is
told, and how, is an owner decision (D11).

    .venv/bin/python support.py list
    .venv/bin/python support.py from-alerts [--since <ISO UTC>]   # new lines of db/alerts.log -> tickets
"""

import hashlib
import re
import sys
from pathlib import Path

import clinic_time

SEVERITIES = ("low", "medium", "high")
ALERT_LOG = Path("db/alerts.log")
IDENTIFYING = re.compile(r"\b[A-Z]{6}[0-9LMNPQRSTUV]{2}[A-Z][0-9LMNPQRSTUV]{2}[A-Z][0-9LMNPQRSTUV]{3}[A-Z]\b"
                         r"|\b[A-Z]{4}[0-9]{12}\b|[\w.+-]+@[\w-]+\.[\w.]+|(?<!\d)3\d{2}[ .]?\d{6,7}(?!\d)", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS support_tickets (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL CHECK (source IN ('manual', 'alert', 'eval', 'drill')),
    summary TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('low', 'medium', 'high')),
    status TEXT NOT NULL CHECK (status IN ('open', 'triaged', 'closed')),
    owner TEXT,
    due_on TEXT,
    resolution TEXT,
    alert_key TEXT UNIQUE,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS support_ticket_events (
    id INTEGER PRIMARY KEY,
    ticket_id INTEGER NOT NULL,
    event TEXT NOT NULL,
    detail TEXT,
    actor TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS support_ticket_events_no_update BEFORE UPDATE ON support_ticket_events
    BEGIN SELECT RAISE(ABORT, 'support ticket history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS support_ticket_events_no_delete BEFORE DELETE ON support_ticket_events
    BEGIN SELECT RAISE(ABORT, 'support ticket history is append-only'); END;
CREATE TABLE IF NOT EXISTS ops_health_samples (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    all_up INTEGER NOT NULL,
    alerts INTEGER NOT NULL,
    checks TEXT NOT NULL
);
"""


class SupportError(Exception):
    pass


def _allowed(conn, actor, role, action):
    from auth import authorize, log_audit
    if role == "system":
        return
    if not authorize(role, "manage_support"):
        log_audit(conn, actor, role, action, "support", allowed=0)
        raise SupportError(f"{role} is not allowed to manage support tickets")


def _event(conn, tid, event, detail, actor):
    conn.execute("INSERT INTO support_ticket_events (ticket_id, event, detail, actor, at) VALUES (?, ?, ?, ?, ?)",
                 (tid, event, detail, actor, clinic_time.stamp()))


def _clean(text, what):
    if not (text or "").strip():
        raise SupportError(f"a {what} is required")
    if IDENTIFYING.search(text):
        raise SupportError(f"the {what} looks identifying (a codice fiscale, e-mail or phone); tickets are about the system")
    return text.strip()


def open_ticket(conn, summary, severity, actor, role, source="manual", alert_key=None):
    _allowed(conn, actor, role, "support_open")
    if severity not in SEVERITIES:
        raise SupportError(f"severity must be one of {SEVERITIES}")
    summary = _clean(summary, "summary")
    tid = conn.execute("INSERT INTO support_tickets (source, summary, severity, status, alert_key, created_by, created_at)"
                       " VALUES (?, ?, ?, 'open', ?, ?, ?)",
                       (source, summary, severity, alert_key, actor, clinic_time.stamp())).lastrowid
    _event(conn, tid, "opened", f"{source}, {severity}", actor)
    conn.commit()
    return tid


def ticket(conn, tid):
    row = conn.execute("SELECT * FROM support_tickets WHERE id = ?", (tid,)).fetchone()
    return dict(row) if row else None


def tickets(conn, status=None):
    sql, args = "SELECT * FROM support_tickets", ()
    if status:
        sql, args = sql + " WHERE status = ?", (status,)
    return [dict(r) for r in conn.execute(sql + " ORDER BY id", args)]


def events(conn, tid):
    return [dict(r) for r in conn.execute("SELECT * FROM support_ticket_events WHERE ticket_id = ? ORDER BY id", (tid,))]


def triage(conn, tid, owner, due_on, actor, role):
    _allowed(conn, actor, role, "support_triage")
    t = ticket(conn, tid)
    if t is None or t["status"] == "closed":
        raise SupportError("only an open ticket can be triaged")
    conn.execute("UPDATE support_tickets SET status = 'triaged', owner = ?, due_on = ? WHERE id = ?", (owner, due_on, tid))
    _event(conn, tid, "triaged", f"owner {owner}, due {due_on}", actor)
    conn.commit()


def close(conn, tid, resolution, actor, role):
    _allowed(conn, actor, role, "support_close")
    t = ticket(conn, tid)
    if t is None:
        raise SupportError("no such ticket")
    if t["status"] == "closed":
        raise SupportError("the ticket is already closed")
    resolution = _clean(resolution, "resolution")
    conn.execute("UPDATE support_tickets SET status = 'closed', resolution = ?, closed_at = ? WHERE id = ?",
                 (resolution, clinic_time.stamp(), tid))
    _event(conn, tid, "closed", resolution, actor)
    conn.commit()


def from_alerts(conn, log=ALERT_LOG, since=None):
    """one ticket per alert line; a line already turned into a ticket is skipped.
    since: an ISO UTC time - older lines are left alone (history is not a backlog)."""
    log = Path(log)
    if not log.exists():
        return []
    made = []
    for line in log.read_text().splitlines():
        if not line.strip():
            continue
        if since and line.split(" | ", 1)[0].strip() < since:
            continue
        key = hashlib.sha256(line.encode()).hexdigest()
        if conn.execute("SELECT 1 FROM support_tickets WHERE alert_key = ?", (key,)).fetchone():
            continue
        what = line.split("|", 1)[1].strip() if "|" in line else line
        severity = "high" if "incident" in what or "filevault" in what else "medium"
        made.append(open_ticket(conn, f"alert: {what}", severity, "system", "system", source="alert", alert_key=key))
    return made


def main(argv):
    import sqlite3
    conn = sqlite3.connect("db/clinic.sqlite")
    conn.row_factory = sqlite3.Row
    try:
        if argv[:1] == ["from-alerts"]:
            since = argv[argv.index("--since") + 1] if "--since" in argv else None
            print(f"{len(from_alerts(conn, since=since))} new ticket(s)")
        elif argv[:1] == ["list"]:
            for t in tickets(conn):
                print(f"#{t['id']} {t['status']:<8} {t['severity']:<6} {t['summary']}")
        else:
            print(__doc__.split("\n\n")[-1])
            return 2
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
