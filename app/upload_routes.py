import os
import tempfile
from pathlib import Path

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

import sort_files
import upload_worker
from auth import authorize, log_audit
from dental_notes_schema import CF_PATTERN
from storage import lookup_patient

from .db import get_db

upload_bp = Blueprint("upload", __name__)

SORTED_ROOT = Path("sorted")
DROP_DIR = Path("drop")
LOG_PATH = "sorted/log.txt"

ALLOWED_EXTS = {".pdf", ".jpg", ".jpeg", ".png", ".txt", ".xlsx"}
MEDIA_EXTS = ALLOWED_EXTS - {".txt"}


def _process_uploads(files, cf, username, role, conn):
    results = []
    for file in files:
        filename = file.filename
        if not filename:
            continue

        ext = os.path.splitext(filename)[1].lower()
        if ext not in ALLOWED_EXTS:
            # a refused upload is still an attempt - audit it like the role
            # denial above, or the log only ever shows what succeeded
            log_audit(conn, username, role, "upload_file", filename, allowed=0)
            results.append({
                "filename": filename,
                "status": "rejected",
                "message": "File type not allowed. Accepted: PDF, JPG, PNG, TXT, XLSX.",
            })
            continue

        safe = secure_filename(filename)
        # secure_filename strips non-ascii, so a name like "приветик.txt"
        # comes back as "txt" - extension gone, and the worker's .txt sync
        # gate never matches. put the checked extension back.
        stem, safe_ext = os.path.splitext(safe)
        if safe_ext.lower() != ext:
            safe = (stem or "upload") + ext
        if cf:
            safe = f"{cf}_{safe}"

        # atomic hand-off (D-05): write to a temp name in the same drop dir,
        # then rename into place so the watcher never sees a half-written file.
        # the name has to be unique and the rename must not overwrite, or two
        # uploads of the same filename clobber each other and the surviving
        # content gets audited under the first uploader's name.
        DROP_DIR.mkdir(parents=True, exist_ok=True)
        final = DROP_DIR / safe
        n = 1
        while final.exists():
            final = DROP_DIR / f"{Path(safe).stem}_{n}{Path(safe).suffix}"
            n += 1
        fd, tmp = tempfile.mkstemp(dir=str(DROP_DIR), suffix=".part")
        os.close(fd)
        file.save(tmp)
        os.rename(tmp, str(final))

        if ext in MEDIA_EXTS:
            dest = sort_files.route_file(final, SORTED_ROOT, LOG_PATH)
            log_audit(conn, username, role, "upload_file", str(dest), allowed=1)
            results.append({
                "filename": filename,
                "status": "sorted",
                "message": f"Filed to {dest.parent.name}.",
            })
        else:
            # the queue is in memory and the worker is a daemon thread, so a
            # restart before it drains would leave no record the file was ever
            # uploaded. this row is that record, and it shows as pending.
            log_audit(conn, username, role, "queue_upload", str(final), allowed=1)
            upload_worker.enqueue(final, username, role)
            results.append({
                "filename": filename,
                "status": "queued",
                "message": "Queued for processing.",
            })
    return results


@upload_bp.route("/patients/<cf>/upload", methods=["POST"])
def submit_patient(cf):
    if not CF_PATTERN.match(cf):
        abort(404)

    if not authorize(g.user["role"], "upload_file"):
        log_audit(get_db(), g.user["username"], g.user["role"], "upload_file", cf, allowed=0)
        flash("You don't have permission to upload files.", "danger")
        return redirect(url_for("patients.detail_view", cf=cf))

    # a fabricated-but-well-formed CF must 404 before any disk write, or a
    # fake CF would create an orphan sorted/<fake-cf>/ dir (WARNING-1)
    if lookup_patient(cf, get_db()) is None:
        abort(404)

    files = request.files.getlist("files")
    results = _process_uploads(files, cf, g.user["username"], g.user["role"], get_db())

    if request.headers.get("HX-Request"):
        return render_template("_upload_results.html", results=results)
    return redirect(url_for("patients.detail_view", cf=cf))


@upload_bp.route("/upload", methods=["POST"])
def submit_dashboard():
    if not authorize(g.user["role"], "upload_file"):
        log_audit(get_db(), g.user["username"], g.user["role"], "upload_file", None, allowed=0)
        flash("You don't have permission to upload files.", "danger")
        return redirect(url_for("dashboard.index"))

    files = request.files.getlist("files")
    results = _process_uploads(files, None, g.user["username"], g.user["role"], get_db())

    if request.headers.get("HX-Request"):
        return render_template("_upload_results.html", results=results)
    return redirect(url_for("dashboard.index"))


def _intake_state(row):
    # needs_review is tested BEFORE allowed on purpose. a file that fails
    # extraction is still an authorised upload, so allowed=1, and the old
    # ordering is exactly what made a failed note render the Sorted badge.
    # the needs_review test matches a path SEGMENT, not a substring - a
    # patient file legitimately named needs_review.txt must not false-positive.
    target = row["target"] or ""
    if row["action"] == "queue_upload":
        return "queued"
    if target.startswith("routed by watcher:"):
        return "external"
    if "needs_review" in Path(target).parts[:-1]:
        return "needs_review"
    if row["allowed"]:
        return "sorted"
    if row["action"] == "sync_note":
        return "not_searchable"
    return "rejected"


def _user_recent_intake(conn, username, limit=10):
    # per-user scoping (D-19) - reads audit_log only, never the shared
    # operational log, which has no user field and would leak clinic-wide
    # filenames (D-02/CR-01). widened to sync_note (D-07) so the badge can
    # report whether a filed note actually landed, not just whether the
    # upload itself was authorized. role != 'system' keeps every watcher and
    # backfill row out of a per-user list structurally (D-11), independent
    # of what any real account happens to be named.
    rows = conn.execute(
        "SELECT ts, target, action, allowed, reason FROM audit_log"
        " WHERE username = ? AND action IN ('queue_upload', 'upload_file', 'sync_note')"
        " AND role != 'system' AND target IS NOT NULL"
        " ORDER BY id DESC LIMIT ?",
        (username, limit * 3),
    ).fetchall()

    # collapse to one row per file, newest first. the three rows a .txt
    # produces sit on different paths - drop/ while queued, sorted/ once
    # filed - so the key is the filename, not the full target: queued is
    # superseded by sorted, which is superseded by the sync outcome.
    # drop names are made unique on upload and sort_files._move renames
    # collisions to <stem>_1, so two distinct files never share a name.
    seen = set()
    collapsed = []
    for row in rows:
        name = Path(row["target"]).name
        if name in seen:
            continue
        seen.add(name)
        collapsed.append({
            "ts": row["ts"],
            "target": row["target"],
            "action": row["action"],
            "allowed": row["allowed"],
            "state": _intake_state(row),
            "reason": row["reason"],
        })
        if len(collapsed) == limit:
            break
    return collapsed


@upload_bp.route("/upload/recent")
def recent_intake():
    # HTMX-polled fragment - a denied fragment returns a bare status
    if not authorize(g.user["role"], "upload_file"):
        return "", 403

    rows = _user_recent_intake(get_db(), g.user["username"])
    return render_template("_recent_intake.html", rows=rows)
