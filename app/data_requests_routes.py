from pathlib import Path

from flask import (Blueprint, abort, flash, g, redirect, render_template, request, send_file,
                   url_for)

import data_rights
import patient_id
from auth import authorize, log_audit
from codice_fiscale import is_valid as is_valid_cf

from .db import get_chroma, get_db

data_requests_bp = Blueprint("data_requests", __name__)

SORTED_ROOT = Path("sorted")
EXPORTS_DIR = data_rights.EXPORTS_DIR


def _refuse(action, target=None):
    log_audit(get_db(), g.user["username"], g.user["role"], action, target, allowed=0)
    flash("You don't have permission to handle data requests.", "danger")
    return redirect(url_for("dashboard.index"))


@data_requests_bp.route("/data-requests")
def index():
    if not authorize(g.user["role"], "manage_data_requests"):
        return _refuse("manage_data_requests")
    conn = get_db()
    # expired exports are deleted whenever someone looks, as well as by the
    # retention sweep, so a forgotten one does not sit on disk
    data_rights.expire_exports(conn, EXPORTS_DIR)
    return render_template("data_requests.html", rows=data_rights.listing(conn))


@data_requests_bp.route("/data-requests/<int:req_id>/review", methods=["POST"])
def review(req_id):
    if not authorize(g.user["role"], "manage_data_requests"):
        return _refuse("review_data_request", str(req_id))
    approve = request.form.get("decision") == "approve"
    row = get_db().execute("SELECT kind FROM data_requests WHERE id = ?", (req_id,)).fetchone()
    if approve and row and row["kind"] == "erasure" and request.form.get("confirm") != "yes":
        flash("Tick the confirmation to erase a patient.", "danger")
        return redirect(url_for("data_requests.index"))
    try:
        collection = get_chroma()
    except Exception:
        collection = None
    ok, message = data_rights.review(
        get_db(), req_id, approve,
        g.user["username"], g.user["role"], request.form.get("reason", ""),
        sorted_root=SORTED_ROOT, exports_dir=EXPORTS_DIR, collection=collection)
    flash(message[0].upper() + message[1:] + ".", "success" if ok else "danger")
    return redirect(url_for("data_requests.index"))


@data_requests_bp.route("/data-requests/<int:req_id>/download")
def download(req_id):
    if not authorize(g.user["role"], "manage_data_requests"):
        return _refuse("download_export", str(req_id))
    conn = get_db()
    path, row = data_rights.export_path(conn, req_id, exports_dir=EXPORTS_DIR)
    if path is None:
        abort(404)
    log_audit(conn, g.user["username"], g.user["role"], "download_export", row["patient_id"],
              allowed=1)
    return send_file(path.resolve(), as_attachment=True,
                     download_name=f"data-request-{req_id}.zip", max_age=0)


@data_requests_bp.route("/patients/<cf>/data-request", methods=["POST"])
def file_for_patient(cf):
    # a request taken at the desk or over the phone, filed on the patient's
    # behalf. it still has to be reviewed like one the patient filed
    if not authorize(g.user["role"], "file_data_request"):
        return _refuse("data_request", cf)
    if not is_valid_cf(cf):
        abort(404)
    conn = get_db()
    pid = patient_id.resolve(conn, cf)
    if pid is None:
        abort(404)
    req = data_rights.file_request(conn, pid, request.form.get("kind", ""),
                                   g.user["username"], g.user["role"],
                                   request.form.get("detail"))
    flash("Request filed for review." if req else "That request could not be filed.",
          "success" if req else "danger")
    return redirect(url_for("patients.detail_view", cf=cf) + "#rights-title")
