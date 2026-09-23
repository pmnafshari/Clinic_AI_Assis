"""Uploaded-note review (POL-9). An upload is a draft until a dentist says so.

WHY. A typed note goes extract -> editable preview -> confirm before it is
saved. An uploaded note used to skip all of that: the model read it and the
result was filed straight into the record, searchable and summarisable. This
module is the missing confirm step.

HOW IT HOLDS. `storage.sync_note_file` is the one road from a sorted note JSON
into `visits` and the search index. A JSON that is not already a visit and was
not written by a confirm step is handed here instead: the original and the
extraction move to staging/<token>/ - outside sorted/, so exports, the files
view and backfill never see them - and a `note_reviews` row says `pending`.

WHAT IS KEPT. The extraction exactly as the model returned it, and the sha256
of the original, both immutable by trigger. A dentist's edits are recorded as
the confirmed fields beside them, never over them. A rejected upload keeps its
files. Nothing here deletes anything; erasure does that.

EXISTING NOTES. Visits filed before this existed are classified once and never
rewritten (`classify_legacy`): a web-*.json visit came through the typed confirm
path, the only writer of that name; anything else is a legacy note awaiting a
dentist, and summaries leave it out until one confirms it.
"""
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
from pathlib import Path

import clinic_time
from auth import authorize, log_audit

CAPABILITY = "review_upload"
STAGING_ROOT = Path("staging")
MAX_ORIGINAL_CHARS = 20000

SCHEMA = """
    CREATE TABLE IF NOT EXISTS note_reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        origin TEXT NOT NULL CHECK (origin IN ('upload', 'legacy')),
        status TEXT NOT NULL CHECK (status IN
            ('pending', 'extraction_failed', 'confirming', 'confirmed', 'rejected')),
        patient_id TEXT,
        codice_fiscale TEXT,
        staged_dir TEXT,
        original_path TEXT,
        original_name TEXT,
        original_sha256 TEXT,
        extraction TEXT,
        extraction_error TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        visit_id INTEGER,
        confirmed_fields TEXT,
        edited INTEGER,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        decided_by TEXT,
        decided_at TEXT,
        decision_reason TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_note_reviews_status ON note_reviews (status, created_at);
    CREATE INDEX IF NOT EXISTS idx_note_reviews_patient ON note_reviews (patient_id);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_note_reviews_one_legacy
        ON note_reviews (visit_id) WHERE origin = 'legacy';
    -- what the model returned, and what was uploaded, are evidence. a first
    -- extraction may be written into an empty column (a retry); after that
    -- neither changes
    CREATE TRIGGER IF NOT EXISTS note_reviews_extraction_fixed
        BEFORE UPDATE OF extraction ON note_reviews
        WHEN OLD.extraction IS NOT NULL AND NEW.extraction IS NOT OLD.extraction
        BEGIN SELECT RAISE(ABORT, 'an extraction is never rewritten'); END;
    CREATE TRIGGER IF NOT EXISTS note_reviews_original_fixed
        BEFORE UPDATE OF original_sha256 ON note_reviews
        WHEN OLD.original_sha256 IS NOT NULL AND NEW.original_sha256 IS NOT OLD.original_sha256
        BEGIN SELECT RAISE(ABORT, 'an original is never rewritten'); END;
    -- one save per preview (P14.T2). the typed-note preview issues a token and
    -- the confirm claims it in one statement, so Back-and-resubmit, a retried
    -- request or a double click saves one note, not two
    CREATE TABLE IF NOT EXISTS note_confirm_tokens (
        token TEXT PRIMARY KEY,
        username TEXT NOT NULL,
        issued_at TEXT NOT NULL,
        used_at TEXT
    );
    -- which visits a person has confirmed, and how. summaries read only these
    CREATE TABLE IF NOT EXISTS visit_reviews (
        visit_id INTEGER PRIMARY KEY,
        method TEXT NOT NULL CHECK (method IN
            ('typed', 'upload_confirmed', 'legacy_typed', 'legacy_confirmed')),
        reviewed_by TEXT,
        reviewed_at TEXT NOT NULL,
        review_id INTEGER
    );
"""

TYPED_NAME = re.compile(r"/notes/web-[^/]*\.json$")


class ReviewError(ValueError):
    """A review step refused. `code` is from a closed list."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _now(now):
    return clinic_time.to_storage(now or clinic_time.now_utc())


def _require(conn, actor, role, action, target):
    if not authorize(role, CAPABILITY):
        log_audit(conn, actor, role, action, target, allowed=0)
        raise PermissionError(f"{role} may not review uploaded notes")


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def staged_files(row):
    """Where a review's files are. An unreadable note stays where it was routed."""
    if row["staged_dir"]:
        folder = Path(row["staged_dir"])
        return {"dir": folder, "original": folder / (row["original_name"] or "original"),
                "extraction": folder / "extraction.json"}
    original = Path(row["original_path"]) if row["original_path"] else None
    return {"dir": None, "original": original, "extraction": None}


def mark_reviewed(conn, visit_id, method, actor, now=None, review_id=None):
    conn.execute("INSERT OR IGNORE INTO visit_reviews (visit_id, method, reviewed_by, reviewed_at,"
                 " review_id) VALUES (?, ?, ?, ?, ?)",
                 (visit_id, method, actor, _now(now), review_id))
    conn.commit()


def reviewed_visit_ids(conn, pid):
    return {r[0] for r in conn.execute(
        "SELECT r.visit_id FROM visit_reviews r JOIN visits v ON v.id = r.visit_id"
        " WHERE v.patient_id = ?", (pid,))}


def awaiting(conn, pid):
    """Notes of this patient that no person has confirmed yet."""
    return conn.execute("SELECT COUNT(*) FROM note_reviews WHERE patient_id = ? AND status IN"
                        " ('pending', 'extraction_failed', 'confirming')", (pid,)).fetchone()[0]


def issue_confirm_token(conn, username, now=None):
    token = secrets.token_hex(16)
    conn.execute("INSERT INTO note_confirm_tokens (token, username, issued_at) VALUES (?, ?, ?)",
                 (token, username, _now(now)))
    conn.commit()
    return token


def claim_confirm_token(conn, token, username, now=None):
    """-> "ok", "used" (already saved once) or "unknown" (missing or not theirs)."""
    claimed = conn.execute(
        "UPDATE note_confirm_tokens SET used_at = ? WHERE token = ? AND username = ?"
        " AND used_at IS NULL", (_now(now), token or "", username)).rowcount
    conn.commit()
    if claimed:
        return "ok"
    used = conn.execute("SELECT 1 FROM note_confirm_tokens WHERE token = ? AND username = ?",
                        (token or "", username)).fetchone()
    return "used" if used else "unknown"


# --- intake ----------------------------------------------------------------

def stage(conn, json_path, actor, role, target=None, now=None):
    """Take a routed note out of sorted/ and hold it for review. -> review id,
    or None when another worker got to this file first."""
    import patient_id
    json_path = Path(json_path)
    folder = Path(STAGING_ROOT) / f"u-{secrets.token_hex(8)}"
    folder.mkdir(parents=True, exist_ok=False)
    try:
        # the rename is the claim: two workers racing on one file, one wins
        os.rename(json_path, folder / "extraction.json")
    except FileNotFoundError:
        folder.rmdir()
        return None
    extraction = (folder / "extraction.json").read_text()
    originals = [p for p in json_path.parent.glob(json_path.stem + ".*") if p.suffix != ".json"]
    original = originals[0] if originals else None
    name = sha = None
    if original is not None:
        name = original.name
        os.rename(original, folder / name)
        sha = _sha(folder / name)
    try:
        cf = json.loads(extraction).get("codice_fiscale")
    except ValueError:
        cf = None
    pid = patient_id.resolve(conn, cf) if cf else None
    cur = conn.execute(
        "INSERT INTO note_reviews (origin, status, patient_id, codice_fiscale, staged_dir,"
        " original_name, original_sha256, extraction, created_by, created_at)"
        " VALUES ('upload', 'pending', ?, ?, ?, ?, ?, ?, ?, ?)",
        (pid, cf, str(folder), name, sha, extraction, actor, _now(now)))
    conn.commit()
    log_audit(conn, actor, role, "note_staged", target or str(json_path), allowed=1,
              reason="awaiting_review")
    return cur.lastrowid


def record_failed(conn, path, reason, actor, role, now=None):
    """An upload the model could not read. It stays where it was routed."""
    path = Path(path)
    cur = conn.execute(
        "INSERT INTO note_reviews (origin, status, original_path, original_name, original_sha256,"
        " extraction_error, created_by, created_at)"
        " VALUES ('upload', 'extraction_failed', ?, ?, ?, ?, ?, ?)",
        (str(path), path.name, _sha(path) if path.exists() else None, reason, actor, _now(now)))
    conn.commit()
    log_audit(conn, actor, role, "note_extraction_failed", f"review:{cur.lastrowid}", allowed=1,
              reason=reason)
    return cur.lastrowid


def classify_legacy(conn):
    """Once per visit, never rewriting it. Returns what it did this run."""
    rows = conn.execute(
        "SELECT v.id, v.patient_id, v.source_path FROM visits v"
        " LEFT JOIN visit_reviews r ON r.visit_id = v.id"
        " LEFT JOIN note_reviews n ON n.visit_id = v.id AND n.origin = 'legacy'"
        " WHERE r.visit_id IS NULL AND n.id IS NULL").fetchall()
    out = {"legacy_typed": 0, "legacy_pending": 0, "skipped_erased": 0}
    stamp = _now(None)
    # init_db runs this, and several connections may open the same database at
    # once. OR IGNORE makes a second classifier of the same visit a no-op, and
    # a busy database is left for the next start rather than holding a lock
    try:
        for row in rows:
            path = row["source_path"] or ""
            if path.startswith("erased:"):
                out["skipped_erased"] += 1
            elif TYPED_NAME.search("/" + path):
                out["legacy_typed"] += conn.execute(
                    "INSERT OR IGNORE INTO visit_reviews (visit_id, method, reviewed_by,"
                    " reviewed_at) VALUES (?, 'legacy_typed', NULL, ?)",
                    (row["id"], stamp)).rowcount
            else:
                out["legacy_pending"] += conn.execute(
                    "INSERT OR IGNORE INTO note_reviews (origin, status, patient_id, visit_id,"
                    " created_by, created_at) VALUES ('legacy', 'pending', ?, ?, 'system', ?)",
                    (row["patient_id"], row["id"], stamp)).rowcount
        conn.commit()
    except sqlite3.OperationalError:
        conn.rollback()
        return None
    return out


# --- review ----------------------------------------------------------------

def _row(conn, review_id):
    row = conn.execute("SELECT * FROM note_reviews WHERE id = ?", (review_id,)).fetchone()
    if row is None:
        raise LookupError("no such review")
    return row


def _claim(conn, review_id, to_status):
    """pending -> to_status in one statement. Two callers, one winner."""
    changed = conn.execute("UPDATE note_reviews SET status = ? WHERE id = ? AND status = 'pending'",
                           (to_status, review_id)).rowcount
    conn.commit()
    if not changed:
        raise ReviewError("not_pending", "this note is no longer waiting for review")


def _fields(extraction, form):
    """The note as confirmed: the extraction, with what the dentist changed."""
    base = json.loads(extraction)
    out = dict(base)
    if "visit_date" in form:
        out["visit_date"] = (form.get("visit_date") or "").strip() or None
    if "procedures" in form:
        out["procedures"] = [p.strip() for p in str(form.get("procedures") or "").split(",")
                             if p.strip()]
    if "clinical_notes" in form:
        out["clinical_notes"] = str(form.get("clinical_notes") or "")
    if "next_appointment" in form:
        out["next_appointment"] = (form.get("next_appointment") or "").strip() or None
    # identity is the upload's. a name or codice fiscale in the form is ignored
    out["codice_fiscale"], out["patient_name"] = base["codice_fiscale"], base["patient_name"]
    return out, out != base


def confirm(conn, review_id, form, actor, role, sorted_root=Path("sorted"), collection=None,
            now=None):
    """-> the visit id. An upload is filed; a legacy note is marked reviewed."""
    _require(conn, actor, role, "note_review_confirm", f"review:{review_id}")
    row = _row(conn, review_id)
    if row["origin"] == "legacy":
        return _confirm_legacy(conn, row, actor, role, now)
    if row["status"] != "pending":
        raise ReviewError("not_pending", "this note is no longer waiting for review")
    import storage
    from dental_notes_schema import DentalNote
    fields, edited = _fields(row["extraction"], form or {})
    try:
        note = DentalNote.model_validate(fields)
    except ValueError as e:
        raise ReviewError("invalid", f"the note is not valid: {e.errors()[0]['msg']}") from None

    _claim(conn, review_id, "confirming")
    try:
        name = f"rev-{review_id}.json"
        storage.save_new_note(note, conn, collection, role, actor, sorted_root=sorted_root,
                              filename=name, review_method="upload_confirmed",
                              review_id=review_id)
        # the record is the visits row. a failed search-index write after it
        # leaves the note filed but not searchable - the same state, and the
        # same backfill repair, as a typed note - and the review still stands
        source = f"{note.codice_fiscale}/notes/{name}"
        found = conn.execute("SELECT id FROM visits WHERE source_path = ?", (source,)).fetchone()
        if found is None:
            raise RuntimeError("the note could not be filed")
        visit_id = found[0]
        mark_reviewed(conn, visit_id, "upload_confirmed", actor, now, review_id)
    except Exception:
        conn.rollback()
        conn.execute("UPDATE note_reviews SET status = 'pending' WHERE id = ?", (review_id,))
        conn.commit()
        log_audit(conn, actor, role, "note_review_confirm", f"review:{review_id}", allowed=0,
                  reason="save_failed")
        raise ReviewError("save_failed", "the note could not be filed; it is still waiting") \
            from None
    import patient_id
    conn.execute("UPDATE note_reviews SET status = 'confirmed', visit_id = ?, confirmed_fields = ?,"
                 " edited = ?, patient_id = ?, decided_by = ?, decided_at = ? WHERE id = ?",
                 (visit_id, json.dumps(fields, sort_keys=True), 1 if edited else 0,
                  patient_id.resolve(conn, note.codice_fiscale), actor, _now(now), review_id))
    conn.commit()
    log_audit(conn, actor, role, "note_review_confirm", f"review:{review_id}", allowed=1)
    if edited:
        log_audit(conn, actor, role, "note_review_edit", f"review:{review_id}", allowed=1)
    return visit_id


def _confirm_legacy(conn, row, actor, role, now):
    _claim(conn, row["id"], "confirmed")
    conn.execute("UPDATE note_reviews SET decided_by = ?, decided_at = ? WHERE id = ?",
                 (actor, _now(now), row["id"]))
    mark_reviewed(conn, row["visit_id"], "legacy_confirmed", actor, now, row["id"])
    log_audit(conn, actor, role, "note_review_confirm", f"review:{row['id']}", allowed=1,
              reason="legacy")
    return row["visit_id"]


def reject(conn, review_id, reason, actor, role, sorted_root=None, collection=None, now=None):
    """Nothing is filed and nothing is deleted. A legacy note keeps its visit
    and stays out of summaries."""
    _require(conn, actor, role, "note_review_reject", f"review:{review_id}")
    row = _row(conn, review_id)
    changed = conn.execute(
        "UPDATE note_reviews SET status = 'rejected', decided_by = ?, decided_at = ?,"
        " decision_reason = ? WHERE id = ? AND status IN ('pending', 'extraction_failed')",
        (actor, _now(now), (reason or "")[:500], row["id"])).rowcount
    conn.commit()
    if not changed:
        raise ReviewError("not_pending", "this note is no longer waiting for review")
    log_audit(conn, actor, role, "note_review_reject", f"review:{review_id}", allowed=1)


def retry(conn, review_id, actor, role, extract=None, sorted_root=None, collection=None,
          now=None):
    """Read an unreadable upload again. Success makes it pending, never filed."""
    import patient_id
    _require(conn, actor, role, "note_review_retry", f"review:{review_id}")
    row = _row(conn, review_id)
    if row["status"] != "extraction_failed":
        raise ReviewError("not_failed", "only a note that could not be read can be retried")
    if extract is None:
        from extract_note import extract_note as extract
    attempts = row["attempts"] + 1
    try:
        text = staged_files(row)["original"].read_text()[:MAX_ORIGINAL_CHARS]
        note = extract(text)
    except Exception:
        conn.execute("UPDATE note_reviews SET attempts = ? WHERE id = ?", (attempts, row["id"]))
        conn.commit()
        log_audit(conn, actor, role, "note_review_retry", f"review:{review_id}", allowed=1,
                  reason="failed")
        raise ReviewError("extraction_failed", "the note still could not be read") from None
    changed = conn.execute(
        "UPDATE note_reviews SET status = 'pending', extraction = ?, codice_fiscale = ?,"
        " patient_id = ?, attempts = ? WHERE id = ? AND status = 'extraction_failed'",
        (note.model_dump_json(), note.codice_fiscale,
         patient_id.resolve(conn, note.codice_fiscale), attempts, row["id"])).rowcount
    conn.commit()
    if not changed:
        raise ReviewError("not_failed", "someone else retried this note")
    log_audit(conn, actor, role, "note_review_retry", f"review:{review_id}", allowed=1,
              reason="extracted")


def queue(conn, limit=100):
    return conn.execute(
        "SELECT n.*, p.patient_name FROM note_reviews n LEFT JOIN patients p"
        " ON p.patient_id = n.patient_id WHERE n.status IN"
        " ('pending', 'extraction_failed', 'confirming')"
        " ORDER BY n.origin DESC, n.created_at LIMIT ?", (limit,)).fetchall()


def counts(conn):
    got = {}
    for row in conn.execute("SELECT origin, status, COUNT(*) FROM note_reviews"
                            " GROUP BY origin, status"):
        got[f"{row[0]}:{row[1]}"] = row[2]
    return got


def detail(conn, review_id, actor, role):
    """One review for the dentist's page: the original text beside the extraction."""
    _require(conn, actor, role, "note_review_read", f"review:{review_id}")
    row = _row(conn, review_id)
    log_audit(conn, actor, role, "note_review_read", f"review:{review_id}", allowed=1)
    original = None
    files = staged_files(row)
    if files["original"] is not None and files["original"].exists():
        original = files["original"].read_text(errors="replace")[:MAX_ORIGINAL_CHARS]
    visit = None
    if row["visit_id"]:
        visit = conn.execute("SELECT * FROM visits WHERE id = ?", (row["visit_id"],)).fetchone()
    patient = conn.execute("SELECT patient_name, codice_fiscale FROM patients WHERE patient_id = ?",
                           (row["patient_id"],)).fetchone() if row["patient_id"] else None
    return {"review": row, "original": original,
            "extraction": json.loads(row["extraction"]) if row["extraction"] else None,
            "visit": visit, "patient": patient}


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        import note_review_selftest
        note_review_selftest.selftest()
        return
    print("usage: python note_review.py --selftest")
    sys.exit(1)


if __name__ == "__main__":
    main()
