"""A patient's files (P25): the dentist's timeline, publication to the portal, and correction.

Two approvals, never one. A document is part of a patient's record once a dentist
confirmed it (P15 upload review, or the identity review of a legacy import). It
reaches that patient's portal only while a separate publication, made by a
dentist for that patient, is live. The publication names the patient: if the
document moves to another record (a correction, a merge) the old publication
stops matching at once, before anyone withdraws it, and is then withdrawn and
recorded.

Every serve checks again: the document is confirmed, belongs to the asking
patient, is published to them, and its bytes still hash to what was confirmed.
"""
import sys

import clinic_time
import codice_fiscale
import documents as docs
import patient_id
from auth import log_audit

SCHEMA = """
    CREATE TABLE IF NOT EXISTS document_publications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id INTEGER NOT NULL,
        patient_id TEXT NOT NULL,
        published_by TEXT NOT NULL,
        published_at TEXT NOT NULL,
        withdrawn_by TEXT,
        withdrawn_at TEXT,
        withdraw_reason TEXT
    );
    -- one live publication per document
    CREATE UNIQUE INDEX IF NOT EXISTS idx_document_publications_live
        ON document_publications (document_id) WHERE withdrawn_at IS NULL;
    CREATE INDEX IF NOT EXISTS idx_document_publications_patient
        ON document_publications (patient_id, withdrawn_at);
    -- legacy import asks "are these bytes already in anyone's record"
    CREATE INDEX IF NOT EXISTS idx_patient_documents_sha ON patient_documents (sha256);
    -- who said which patient a document belongs to, and who published or withdrew it
    CREATE TABLE IF NOT EXISTS document_identity_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id INTEGER NOT NULL,
        action TEXT NOT NULL CHECK (action IN ('attached', 'corrected', 'published', 'withdrawn')),
        from_pid TEXT,
        to_pid TEXT,
        actor TEXT NOT NULL,
        at TEXT NOT NULL,
        reason TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_document_identity_events_doc
        ON document_identity_events (document_id);
    CREATE TRIGGER IF NOT EXISTS document_identity_events_fixed
        BEFORE UPDATE ON document_identity_events
        BEGIN SELECT RAISE(ABORT, 'an identity event is never rewritten'); END;
"""

# M25: added to P15's table. Legacy rows read NULL: unknown, never guessed
COLUMNS = (("category", "TEXT"), ("source", "TEXT"), ("import_item_id", "INTEGER"),
           ("acquired_at", "TEXT"), ("acquired_basis", "TEXT"), ("visit_id", "INTEGER"))

CATEGORIES = {"opg": "OPG", "xray": "X-ray", "photo": "Photo", "report": "Report",
              "letter": "Letter", "consent": "Consent form", "other": "Document"}
FILE_TYPES = {"pdf": "PDF", "png": "PNG image", "jpeg": "JPEG image", "text": "Text"}
PREVIEWABLE = ("png", "jpeg")
DATE_BASIS = {"document_date": "date written on the document", "exif_original": "camera time (EXIF)",
              "reviewer": "date given by the reviewer"}

def ensure_columns(conn):
    have = {r[1] for r in conn.execute("PRAGMA table_info(patient_documents)")}
    for name, kind in COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE patient_documents ADD COLUMN {name} {kind}")
    conn.commit()


def _now(now):
    return clinic_time.to_storage(now or clinic_time.now_utc())


def _event(conn, doc_id, action, from_pid, to_pid, actor, now, reason=None):
    conn.execute("INSERT INTO document_identity_events (document_id, action, from_pid, to_pid, actor,"
                 " at, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (doc_id, action, from_pid, to_pid, actor, _now(now), (reason or "")[:300] or None))


# --- publication ---------------------------------------------------------------

def published(conn, doc_id, pid):
    """The portal's own rule (patient_accessor.PUBLISHED), asked from the staff side."""
    import patient_accessor
    return patient_accessor.get_published_file(pid, doc_id, conn) is not None


def publish(conn, doc_id, pid, actor, role, now=None):
    """A dentist shows one confirmed document to its own patient. Twice is once."""
    docs._require(conn, actor, role, "document_publish", f"document:{doc_id}")
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        r = conn.execute("SELECT status FROM patient_documents WHERE id = ? AND patient_id = ?",
                         (doc_id, pid)).fetchone()
        if r is None:
            raise LookupError("no such document for this patient")
        if r["status"] != "confirmed":
            raise docs.DocumentError("not_confirmed", "only a confirmed document can be shown to the patient")
        live = conn.execute("SELECT patient_id FROM document_publications WHERE document_id = ?"
                            " AND withdrawn_at IS NULL", (doc_id,)).fetchone()
        if live and live["patient_id"] == pid:
            conn.rollback()
            return
        if live:
            # published to a record it no longer belongs to: close that first
            conn.execute("UPDATE document_publications SET withdrawn_by = ?, withdrawn_at = ?,"
                         " withdraw_reason = 'belongs to another record' WHERE document_id = ?"
                         " AND withdrawn_at IS NULL", (actor, _now(now), doc_id))
        conn.execute("INSERT INTO document_publications (document_id, patient_id, published_by,"
                     " published_at) VALUES (?, ?, ?, ?)", (doc_id, pid, actor, _now(now)))
        _event(conn, doc_id, "published", None, pid, actor, now)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    log_audit(conn, actor, role, "document_publish", f"document:{doc_id}", allowed=1)


def _withdraw_live(conn, doc_id, actor, reason, now):
    """Inside the caller's transaction. -> the patient it was published to, or None."""
    live = conn.execute("SELECT patient_id FROM document_publications WHERE document_id = ?"
                        " AND withdrawn_at IS NULL", (doc_id,)).fetchone()
    if live is None:
        return None
    conn.execute("UPDATE document_publications SET withdrawn_by = ?, withdrawn_at = ?,"
                 " withdraw_reason = ? WHERE document_id = ? AND withdrawn_at IS NULL",
                 (actor, _now(now), (reason or "")[:300] or None, doc_id))
    _event(conn, doc_id, "withdrawn", live["patient_id"], None, actor, now, reason)
    return live["patient_id"]


def withdraw(conn, doc_id, pid, reason, actor, role, now=None):
    docs._require(conn, actor, role, "document_withdraw", f"document:{doc_id}")
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT 1 FROM patient_documents WHERE id = ? AND patient_id = ?",
                        (doc_id, pid)).fetchone() is None:
            raise LookupError("no such document for this patient")
        _withdraw_live(conn, doc_id, actor, reason, now)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    log_audit(conn, actor, role, "document_withdraw", f"document:{doc_id}", allowed=1)


# --- correction ------------------------------------------------------------------

def correct(conn, doc_id, from_pid, to_key, reason, actor, role, now=None):
    """The document was put in the wrong record. It moves; its publication ends; it is recorded.

    The file itself is never rewritten. If the right record already holds the same
    bytes, this copy is superseded instead of becoming a second one."""
    docs._require(conn, actor, role, "document_correct", f"document:{doc_id}")
    reason = (reason or "").strip()
    if not reason:
        raise docs.DocumentError("reason_needed", "say why the document belongs to another patient")
    key = (to_key or "").strip()
    to_pid = patient_id.resolve(conn, key) or patient_id.resolve(conn, codice_fiscale.normalize(key))
    if to_pid is None:
        raise docs.DocumentError("no_patient", "no patient with that code")
    if to_pid == from_pid:
        raise docs.DocumentError("same_patient", "the document is already in that record")
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        r = conn.execute("SELECT * FROM patient_documents WHERE id = ? AND patient_id = ?",
                         (doc_id, from_pid)).fetchone()
        if r is None:
            raise LookupError("no such document for this patient")
        if r["status"] not in ("confirmed", "pending_review"):
            raise docs.DocumentError("not_current", "only a current document can be moved")
        _withdraw_live(conn, doc_id, actor, "moved to another record", now)
        twin = conn.execute("SELECT id FROM patient_documents WHERE patient_id = ? AND sha256 = ?"
                            " AND status NOT IN ('rejected', 'superseded')", (to_pid, r["sha256"])).fetchone()
        if twin:
            conn.execute("UPDATE patient_documents SET status = 'superseded', reason = ? WHERE id = ?",
                         (f"wrong record; the right one already holds it as document:{twin[0]}", doc_id))
        else:
            conn.execute("UPDATE patient_documents SET patient_id = ?, visit_id = NULL WHERE id = ?",
                         (to_pid, doc_id))
        if _has(conn, "import_items"):
            conn.execute("UPDATE import_items SET patient_id = ? WHERE document_id = ?", (to_pid, doc_id))
        _event(conn, doc_id, "corrected", from_pid, to_pid, actor, now, reason)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    _reindex(conn, doc_id)
    log_audit(conn, actor, role, "document_correct", f"document:{doc_id}", allowed=1,
              reason=f"{from_pid} -> {to_pid}")


def _reindex(conn, doc_id):
    # the database is what search checks; an index that cannot be reached is rebuilt later
    try:
        docs.unindex([doc_id])
        r = docs.row(conn, doc_id)
        if r["status"] == "confirmed":
            docs._index(r)
    except docs.DocumentError:
        pass


def _has(conn, table):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (table,)).fetchone() is not None


def history(conn, doc_id, actor, role):
    docs._require(conn, actor, role, "document_read", f"document:{doc_id}")
    return [dict(r) for r in conn.execute("SELECT * FROM document_identity_events WHERE document_id = ?"
                                          " ORDER BY id", (doc_id,))]


# --- what the dentist sees ------------------------------------------------------------

def label(r):
    if r["category"] in CATEGORIES:
        return CATEGORIES[r["category"]]
    return {"png": "Image", "jpeg": "Image", "pdf": "PDF document", "text": "Text document"}.get(
        r["kind"], "File")


def dated(r):
    """(clinic date, what that date is). An acquisition date only when the evidence gave one."""
    if r["acquired_at"]:
        return clinic_time.local_date(r["acquired_at"]), DATE_BASIS.get(r["acquired_basis"], "acquired")
    return clinic_time.local_date(r["uploaded_at"]), ("imported" if r["source"] == "legacy_import"
                                                       else "uploaded")


def timeline(conn, pid, actor, role, limit=200):
    """Confirmed files and confirmed notes of one patient, newest first. Dentist only."""
    docs._require(conn, actor, role, "file_timeline", f"patient:{pid}")
    log_audit(conn, actor, role, "file_timeline", f"patient:{pid}", allowed=1)
    out = []
    for r in conn.execute(
            # the staff badge; the portal's own rule is patient_accessor.PUBLISHED
            "SELECT d.*, v.visit_date, p.id AS publication FROM patient_documents d"
            " LEFT JOIN visits v ON v.id = d.visit_id AND v.patient_id = d.patient_id"
            " LEFT JOIN document_publications p ON p.document_id = d.id AND p.patient_id = d.patient_id"
            " AND p.withdrawn_at IS NULL"
            " WHERE d.patient_id = ? AND d.status = 'confirmed' ORDER BY d.id DESC LIMIT ?", (pid, limit)):
        date, basis = dated(r)
        if r["source"] == "legacy_import":
            source = f"legacy import, batch {_batch_of(conn, r['import_item_id'])}"
        else:
            source = f"uploaded by {r['uploaded_by']}"
        out.append({"kind": "file", "doc_id": r["id"], "date": date, "date_basis": basis,
                    "category": label(r), "file_type": FILE_TYPES.get(r["kind"], "File"),
                    "display_name": r["display_name"], "source": source,
                    "confirmed_by": r["decided_by"], "visit_id": r["visit_id"], "visit_date": r["visit_date"],
                    "published": r["publication"] is not None, "preview": r["kind"] in PREVIEWABLE})
    for v in conn.execute(
            "SELECT v.id, v.visit_date, v.procedures, r.reviewed_by FROM visits v"
            " JOIN visit_reviews r ON r.visit_id = v.id WHERE v.patient_id = ?"
            " ORDER BY v.visit_date DESC LIMIT ?", (pid, limit)):
        out.append({"kind": "note", "visit_id": v["id"], "date": v["visit_date"] or "",
                    "date_basis": "visit date", "category": "Visit note", "file_type": "Note",
                    "display_name": _procedures(v["procedures"]), "source": f"confirmed by {v['reviewed_by']}",
                    "visit_date": v["visit_date"], "published": False, "preview": False})
    out.sort(key=lambda e: (e["date"] or "", e.get("doc_id") or 0), reverse=True)
    return out


def _batch_of(conn, item_id):
    r = conn.execute("SELECT batch_id FROM import_items WHERE id = ?", (item_id,)).fetchone()
    return r[0] if r else "?"


def _procedures(raw):
    import json
    try:
        items = json.loads(raw or "[]")
    except ValueError:
        items = []
    return ", ".join(items) if items else "Visit note"


def preview(conn, doc_id, pid, actor, role):
    """(bytes, mimetype) of an image the dentist may look at. For orientation, not diagnosis."""
    r = docs.load(conn, doc_id, pid, actor, role)
    if r["kind"] not in PREVIEWABLE:
        raise docs.DocumentError("no_preview", "no preview for this type; download the original")
    data = docs.verified_bytes(r)
    if data is None:
        raise docs.DocumentError("changed", "the stored file does not match what was confirmed")
    return data, docs.MIMETYPES[r["kind"]]


# --- life cycle ----------------------------------------------------------------------

def on_merge(conn, source_pid, actor, now=None):
    """Inside the merge transaction: nothing published under the folded record carries over."""
    ids = [r[0] for r in conn.execute("SELECT document_id FROM document_publications WHERE patient_id = ?"
                                      " AND withdrawn_at IS NULL", (source_pid,))]
    for doc_id in ids:
        _withdraw_live(conn, doc_id, actor, "records merged; publish again if right", now)
    return ids


def on_erase(conn, pid):
    """Inside the erasure transaction, before the documents go."""
    conn.execute("DELETE FROM document_publications WHERE patient_id = ? OR document_id IN"
                 " (SELECT id FROM patient_documents WHERE patient_id = ?)", (pid, pid))
    conn.execute("DELETE FROM document_identity_events WHERE from_pid = ? OR to_pid = ? OR document_id IN"
                 " (SELECT id FROM patient_documents WHERE patient_id = ?)", (pid, pid, pid))


def remaining(conn, pid):
    return conn.execute("SELECT COUNT(*) FROM document_publications WHERE patient_id = ?",
                        (pid,)).fetchone()[0]


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        import patient_files_selftest
        patient_files_selftest.selftest()
