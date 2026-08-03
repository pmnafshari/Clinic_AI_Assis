import os
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
        # then rename into place so the watcher never sees a half-written file
        DROP_DIR.mkdir(parents=True, exist_ok=True)
        tmp = DROP_DIR / (safe + ".part")
        file.save(str(tmp))
        final = DROP_DIR / safe
        os.rename(str(tmp), str(final))

        if ext in MEDIA_EXTS:
            dest = sort_files.route_file(final, SORTED_ROOT, LOG_PATH)
            log_audit(conn, username, role, "upload_file", str(dest), allowed=1)
            results.append({
                "filename": filename,
                "status": "sorted",
                "message": f"Filed to {dest.parent.name}.",
            })
        else:
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


def _user_recent_intake(conn, username, limit=10):
    # per-user scoping (D-19) - reads audit_log only, never the shared
    # operational log, which has no user field and would leak clinic-wide
    # filenames (D-02/CR-01). widened to sync_note (D-07) so the badge can
    # report whether a filed note actually landed, not just whether the
    # upload itself was authorized. role != 'system' keeps every watcher and
    # backfill row out of a per-user list structurally (D-11), independent
    # of what any real account happens to be named.
    rows = conn.execute(
        "SELECT ts, target, action, allowed FROM audit_log"
        " WHERE username = ? AND action IN ('upload_file', 'sync_note') AND role != 'system'"
        " ORDER BY id DESC LIMIT ?",
        (username, limit * 3),
    ).fetchall()

    # collapse to one row per file, newest first: a .txt's upload_file row
    # shows "Sorted" until the worker's later sync_note row supersedes it in
    # place. two rows sharing a target otherwise only means a repeated
    # rejection of the same filename - sort_files._move renames real
    # collisions to <stem>_1, so this never merges two distinct filed notes.
    seen = set()
    collapsed = []
    for row in rows:
        if row["target"] in seen:
            continue
        seen.add(row["target"])
        collapsed.append(row)
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
