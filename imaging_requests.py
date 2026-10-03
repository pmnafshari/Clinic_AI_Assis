"""Demo imaging requests (P26): a dentist's patient-linked request, and reception's read-only handoff.

SYNTHETIC DEMO. A "demo imaging request" is not a radiology order, a prescription or an authorised exposure;
nothing is sent, booked or billed anywhere.

THE DENTIST DECIDES, EXPLICITLY. Only a dentist creates a request, for the one patient whose record is open, and
it does nothing until the dentist activates it in a separate step. A correction is a new version that replaces the
active one only when the dentist activates it; nothing active is ever edited in place. Cancellation is immediate.
Every transition runs in one write transaction that checks the role, the patient, the state and the version, so
a second click, a stale page or a competing dentist changes nothing twice and cannot bring back a cancelled or
replaced request.

RECEPTION READS, NEVER CHOOSES. Reception sees the active requests of the patient it is looking at, each on its
own line, exactly as recorded, and may acknowledge one or ask the dentist about it. No type is ever chosen from a
question, a symptom, a verbal claim or a model: there is no such logic here. General steps come from an approved
P24 procedure through a fixed question per type that carries nothing about the patient.
"""
import sys

import clinic_time
from auth import authorize, log_audit

MANAGE, VIEW = "manage_imaging_requests", "view_imaging_requests"
# for synthetic tests only; not a clinical catalogue. "other" takes the dentist's own label
EXAMS = {"opg": "OPG (panoramic) - demo", "bitewing": "Bitewing pair - demo", "periapical": "Periapical - demo",
         "cbct": "CBCT - demo", "other": None}
# the one question per type sent to Ask clinic guides: fixed text, no patient in it
GUIDE_QUESTIONS = {"opg": "The dentist has already ordered an OPG. What does reception do next?"}
NO_REQUEST = "No active imaging request is recorded for this patient; ask the dentist."

SCHEMA = """
    CREATE TABLE IF NOT EXISTS imaging_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        series_id INTEGER,
        version INTEGER NOT NULL,
        patient_id TEXT NOT NULL,
        exam TEXT NOT NULL CHECK (exam IN ('opg', 'bitewing', 'periapical', 'cbct', 'other')),
        exam_label TEXT NOT NULL,
        note TEXT,
        state TEXT NOT NULL CHECK (state IN ('draft', 'active', 'cancelled', 'superseded')),
        created_by TEXT NOT NULL, created_at TEXT NOT NULL,
        activated_by TEXT, activated_at TEXT,
        ended_by TEXT, ended_at TEXT, end_reason TEXT,
        supersedes_id INTEGER,
        submit_token TEXT NOT NULL UNIQUE
    );
    CREATE INDEX IF NOT EXISTS idx_imaging_requests_patient ON imaging_requests (patient_id, state);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_imaging_requests_version ON imaging_requests (series_id, version);
    -- a patient's imaging exam label, version and state are fixed once written: only the state moves on
    CREATE TRIGGER IF NOT EXISTS imaging_requests_fixed BEFORE UPDATE OF exam, exam_label, note, version, created_by
        ON imaging_requests BEGIN SELECT RAISE(ABORT, 'a request version is never edited: revise it'); END;
    CREATE TABLE IF NOT EXISTS imaging_request_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id INTEGER NOT NULL, series_id INTEGER NOT NULL, version INTEGER NOT NULL,
        patient_id TEXT NOT NULL,
        action TEXT NOT NULL, actor TEXT NOT NULL, role TEXT NOT NULL, at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_imaging_request_events_series ON imaging_request_events (series_id);
    CREATE TRIGGER IF NOT EXISTS imaging_request_events_fixed BEFORE UPDATE OF request_id, action, actor, at
        ON imaging_request_events BEGIN SELECT RAISE(ABORT, 'history is never rewritten'); END;
    CREATE TABLE IF NOT EXISTS imaging_request_files (
        request_id INTEGER NOT NULL, document_id INTEGER NOT NULL, patient_id TEXT NOT NULL,
        linked_by TEXT NOT NULL, linked_at TEXT NOT NULL,
        PRIMARY KEY (request_id, document_id)
    );
"""


class RequestError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _now(now=None):
    return clinic_time.to_storage(now or clinic_time.now_utc())


def _require(conn, actor, role, capability, action, target):
    if not authorize(role, capability):
        log_audit(conn, actor, role, action, target, allowed=0)
        raise PermissionError(f"{role} may not {action.replace('_', ' ')}")


def _event(conn, r, action, actor, role, now=None):
    conn.execute("INSERT INTO imaging_request_events (request_id, series_id, version, patient_id, action, actor, role, at)"
                 " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (r["id"], r["series_id"], r["version"], r["patient_id"], action, actor, role, _now(now)))


def get(conn, request_id):
    return conn.execute("SELECT * FROM imaging_requests WHERE id = ?", (request_id,)).fetchone()


def _mine(conn, request_id, pid):
    """The request only if it belongs to this patient; anything else looks exactly like a missing one."""
    r = conn.execute("SELECT * FROM imaging_requests WHERE id = ? AND patient_id = ?", (request_id, pid)).fetchone()
    if r is None:
        raise LookupError("no such request for this patient")
    return r


def _label(exam, label):
    if exam not in EXAMS:
        raise RequestError("exam", "choose a type from the list")
    if exam != "other":
        return EXAMS[exam]
    label = " ".join((label or "").split())[:80]
    if not label:
        raise RequestError("label", "write the type you mean")
    return f"{label} (dentist's label) - demo"


def _duplicates(conn, pid, exam, rid):
    rows = conn.execute("SELECT id, state FROM imaging_requests WHERE patient_id = ? AND exam = ? AND id != ?"
                        " AND state IN ('draft', 'active') ORDER BY id", (pid, exam, rid)).fetchall()
    return [f"A request of this type is already open for this patient (request {r['id']}, {r['state']}): check it is"
            " not a duplicate." for r in rows]


def _write(conn, fn):
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        out = fn()
        conn.commit()
        return out
    except BaseException:
        conn.rollback()
        raise


def create_draft(conn, pid, exam, label, note, actor, role, token, now=None):
    """A new request for this patient, as a draft. The same one-time token twice is one draft. -> (id, warnings)"""
    _require(conn, actor, role, MANAGE, "imaging_create", f"patient:{pid}")
    exam_label = _label(exam, label)
    if not token:
        raise RequestError("token", "the form has expired: open the record again")

    def do():
        seen = conn.execute("SELECT id, patient_id FROM imaging_requests WHERE submit_token = ?", (token,)).fetchone()
        if seen:
            if seen["patient_id"] != pid:
                raise RequestError("token", "the form has expired: open the record again")
            return seen["id"], True
        if conn.execute("SELECT 1 FROM patients WHERE patient_id = ?", (pid,)).fetchone() is None:
            raise LookupError("no such patient")
        cur = conn.execute("INSERT INTO imaging_requests (version, patient_id, exam, exam_label, note, state, created_by,"
                           " created_at, submit_token) VALUES (1, ?, ?, ?, ?, 'draft', ?, ?, ?)",
                           (pid, exam, exam_label, (note or "").strip()[:300] or None, actor, _now(now), token))
        rid = cur.lastrowid
        conn.execute("UPDATE imaging_requests SET series_id = ? WHERE id = ?", (rid, rid))
        _event(conn, get(conn, rid), "created", actor, role, now)
        return rid, False
    rid, repeated = _write(conn, do)
    if not repeated:
        log_audit(conn, actor, role, "imaging_create", f"imaging:{rid}", allowed=1, reason="v1")
    return rid, _duplicates(conn, pid, exam, rid)


def revise(conn, request_id, pid, exam, label, note, actor, role, token, now=None):
    """A correction of an active request: a new draft version. The active one stays until this is activated."""
    _require(conn, actor, role, MANAGE, "imaging_revise", f"imaging:{request_id}")
    exam_label = _label(exam, label)

    def do():
        r = _mine(conn, request_id, pid)
        seen = conn.execute("SELECT id FROM imaging_requests WHERE submit_token = ?", (token,)).fetchone()
        if seen:
            return seen["id"]
        if r["state"] != "active":
            raise RequestError("not_current", "only the active version can be revised")
        import imaging_bookings
        imaging_bookings.refuse_if_completed(conn, r)
        version = conn.execute("SELECT MAX(version) FROM imaging_requests WHERE series_id = ?",
                               (r["series_id"],)).fetchone()[0] + 1
        cur = conn.execute("INSERT INTO imaging_requests (series_id, version, patient_id, exam, exam_label, note, state,"
                           " created_by, created_at, supersedes_id, submit_token) VALUES (?, ?, ?, ?, ?, ?, 'draft', ?,"
                           " ?, ?, ?)", (r["series_id"], version, pid, exam, exam_label, (note or "").strip()[:300] or None,
                                         actor, _now(now), request_id, token))
        _event(conn, get(conn, cur.lastrowid), "revised", actor, role, now)
        return cur.lastrowid
    rid = _write(conn, do)
    log_audit(conn, actor, role, "imaging_revise", f"imaging:{rid}", allowed=1)
    return rid, _duplicates(conn, pid, exam, rid)


def activate(conn, request_id, pid, expected_version, actor, role, now=None):
    """The dentist's explicit step. A revision replaces the version it was made from, if that is still active."""
    _require(conn, actor, role, MANAGE, "imaging_activate", f"imaging:{request_id}")

    def do():
        r = _mine(conn, request_id, pid)
        if r["state"] != "draft":
            raise RequestError("not_draft", "this request is not a draft any more: reload it")
        if r["version"] != int(expected_version):
            raise RequestError("stale", "the request changed since this page was opened: reload it")
        if conn.execute("SELECT 1 FROM patients WHERE patient_id = ?", (pid,)).fetchone() is None:
            raise LookupError("no such patient")
        if r["supersedes_id"]:
            old = get(conn, r["supersedes_id"])
            if old is None or old["state"] != "active":
                raise RequestError("stale", "the version this revises is no longer active: revise the current one")
            conn.execute("UPDATE imaging_requests SET state = 'superseded', ended_by = ?, ended_at = ?,"
                         " end_reason = ? WHERE id = ? AND state = 'active'",
                         (actor, _now(now), f"replaced by version {r['version']}", old["id"]))
            _event(conn, old, "superseded", actor, role, now)
            # P27-D1: its appointment stays; the booking waits for reception, never moves by itself
            import imaging_bookings
            imaging_bookings.on_request_ended(conn, old, "request_revised", now)
        conn.execute("UPDATE imaging_requests SET state = 'active', activated_by = ?, activated_at = ? WHERE id = ?"
                     " AND state = 'draft'", (actor, _now(now), request_id))
        _event(conn, r, "activated", actor, role, now)
        return r["version"]
    version = _write(conn, do)
    log_audit(conn, actor, role, "imaging_activate", f"imaging:{request_id}", allowed=1, reason=f"v{version}")


def cancel(conn, request_id, pid, expected_version, reason, actor, role, now=None):
    _require(conn, actor, role, MANAGE, "imaging_cancel", f"imaging:{request_id}")

    def do():
        r = _mine(conn, request_id, pid)
        if r["state"] not in ("draft", "active"):
            raise RequestError("not_open", "this request is already cancelled or replaced")
        if r["version"] != int(expected_version):
            raise RequestError("stale", "the request changed since this page was opened: reload it")
        import imaging_bookings
        imaging_bookings.refuse_if_completed(conn, r)
        imaging_bookings.on_request_ended(conn, r, "request_cancelled", now)
        conn.execute("UPDATE imaging_requests SET state = 'cancelled', ended_by = ?, ended_at = ?, end_reason = ?"
                     " WHERE id = ?", (actor, _now(now), (reason or "").strip()[:200] or None, request_id))
        _event(conn, r, "cancelled", actor, role, now)
        return r["version"]
    version = _write(conn, do)
    log_audit(conn, actor, role, "imaging_cancel", f"imaging:{request_id}", allowed=1, reason=f"v{version}")


# --- reading ---------------------------------------------------------------------------------------------

def for_patient(conn, pid, actor, role):
    """Dentist: every version. Reception: the active requests only, one line each."""
    _require(conn, actor, role, VIEW, "imaging_read", f"patient:{pid}")
    if authorize(role, MANAGE):
        return conn.execute("SELECT * FROM imaging_requests WHERE patient_id = ? ORDER BY series_id DESC, version DESC",
                            (pid,)).fetchall()
    return conn.execute("SELECT * FROM imaging_requests WHERE patient_id = ? AND state = 'active' ORDER BY"
                        " activated_at, id", (pid,)).fetchall()


def handoff(conn, pid, request_id, actor, role):
    """What reception may act on for this request right now, read from the record itself."""
    _require(conn, actor, role, VIEW, "imaging_read", f"imaging:{request_id}")
    r = _mine(conn, request_id, pid)
    log_audit(conn, actor, role, "imaging_read", f"imaging:{request_id}", allowed=1)
    out = {"request": r, "current": None, "replaced_by": None, "message": ""}
    if r["state"] == "active":
        out["current"] = r["id"]
    elif r["state"] == "draft":
        out["message"] = "This request is a draft: the dentist has not activated it. There is nothing to do; ask the dentist."
    elif r["state"] == "cancelled":
        out["message"] = (f"This request was cancelled by {r['ended_by']}. There is nothing to do; ask the dentist if in"
                          " doubt.")
    else:
        newer = conn.execute("SELECT id FROM imaging_requests WHERE series_id = ? AND state = 'active'",
                             (r["series_id"],)).fetchone()
        out["replaced_by"] = newer["id"] if newer else None
        out["message"] = "This version was superseded by a later one. Open the current version; do not act on this one."
    return out


def history(conn, request_id, pid, actor, role):
    _require(conn, actor, role, VIEW, "imaging_read", f"imaging:{request_id}")
    r = _mine(conn, request_id, pid)
    return [dict(e) for e in conn.execute("SELECT request_id, version, action, actor, role, at FROM"
                                          " imaging_request_events WHERE series_id = ? ORDER BY id", (r["series_id"],))]


def acknowledge(conn, request_id, pid, expected_version, actor, role, now=None):
    """Reception notes it has seen the current version. Once per person; refused if the request moved on."""
    _require(conn, actor, role, VIEW, "imaging_acknowledge", f"imaging:{request_id}")

    def do():
        r = _mine(conn, request_id, pid)
        if r["state"] != "active":
            raise RequestError("not_active", "this request is not active: reload it")
        if r["version"] != int(expected_version):
            raise RequestError("stale", "the request changed since this page was opened: reload it")
        if not conn.execute("SELECT 1 FROM imaging_request_events WHERE request_id = ? AND action = 'acknowledged'"
                            " AND actor = ?", (request_id, actor)).fetchone():
            _event(conn, r, "acknowledged", actor, role, now)
    _write(conn, do)
    log_audit(conn, actor, role, "imaging_acknowledge", f"imaging:{request_id}", allowed=1)


def ask_dentist(conn, request_id, pid, actor, role, now=None):
    """Reception flags a question about this request; the dentist sees it in the history. Nothing is sent."""
    _require(conn, actor, role, VIEW, "imaging_question", f"imaging:{request_id}")
    _write(conn, lambda: _event(conn, _mine(conn, request_id, pid), "question", actor, role, now))
    log_audit(conn, actor, role, "imaging_question", f"imaging:{request_id}", allowed=1)


def guidance(guides_conn, exam, role):
    """Approved generic steps for this type from Ask clinic guides, or none. The question carries no patient."""
    import clinic_guides
    question = GUIDE_QUESTIONS.get(exam)
    if question is None:
        return {"citation": None, "warnings": [], "why": "No approved procedure covers this type."}
    r = clinic_guides.ask(guides_conn, question, role)
    if r["outcome"] != "answer":
        return {"citation": None, "warnings": [], "why": r["message"]}
    return {"citation": r["citations"][0], "warnings": r["warnings"], "why": ""}


# --- files (P25 identity first, then a dentist's link) ----------------------------------------------------

def link_file(conn, request_id, pid, document_id, actor, role, verified=False, now=None):
    """Tie a document P25 already confirmed for this patient to this request. Publishes nothing."""
    _require(conn, actor, role, MANAGE, "imaging_link", f"imaging:{request_id}")
    if not verified:
        raise RequestError("not_verified", "tick that the patient, the request and the file are the right ones")

    def do():
        r = _mine(conn, request_id, pid)
        if r["state"] != "active":
            raise RequestError("not_active", "a file is linked to an active request only")
        d = conn.execute("SELECT status FROM patient_documents WHERE id = ? AND patient_id = ?",
                         (document_id, pid)).fetchone()
        if d is None:
            raise LookupError("no such document for this patient")
        if d["status"] != "confirmed":
            raise RequestError("not_confirmed", "only a file confirmed for this patient can be linked")
        if conn.execute("INSERT OR IGNORE INTO imaging_request_files (request_id, document_id, patient_id, linked_by,"
                        " linked_at) VALUES (?, ?, ?, ?, ?)", (request_id, document_id, pid, actor, _now(now))).rowcount:
            _event(conn, r, "file_linked", actor, role, now)
    _write(conn, do)
    log_audit(conn, actor, role, "imaging_link", f"imaging:{request_id}", allowed=1, reason=f"document:{document_id}")


def linked_files(conn, request_id, pid, actor, role):
    _require(conn, actor, role, MANAGE, "imaging_read", f"imaging:{request_id}")
    _mine(conn, request_id, pid)
    return [dict(r) for r in conn.execute(
        "SELECT f.document_id, f.linked_by, f.linked_at, d.display_name, d.kind FROM imaging_request_files f"
        " JOIN patient_documents d ON d.id = f.document_id AND d.patient_id = f.patient_id"
        " WHERE f.request_id = ? AND f.patient_id = ? ORDER BY f.linked_at", (request_id, pid))]


def linkable_files(conn, pid):
    return conn.execute("SELECT id, display_name, kind, uploaded_at FROM patient_documents WHERE patient_id = ? AND"
                        " status = 'confirmed' ORDER BY id DESC LIMIT 100", (pid,)).fetchall()


# --- life cycle -----------------------------------------------------------------------------------------

def on_erase(conn, pid):
    """Inside the erasure transaction."""
    for table in ("imaging_request_files", "imaging_request_events", "imaging_requests"):
        conn.execute(f"DELETE FROM {table} WHERE patient_id = ?", (pid,))


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        import imaging_requests_selftest
        imaging_requests_selftest.selftest()
