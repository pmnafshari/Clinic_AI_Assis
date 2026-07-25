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
            results.append({
                "filename": filename,
                "status": "rejected",
                "message": "File type not allowed. Accepted: PDF, JPG, PNG, TXT, XLSX.",
            })
            continue

        safe = secure_filename(filename)
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
