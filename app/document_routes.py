import json

from flask import (Blueprint, abort, flash, g, redirect, render_template, request, send_file,
                   url_for)

import documents as docs
import patient_id
from auth import authorize, log_audit
from codice_fiscale import is_valid as is_valid_cf
from storage import lookup_patient

from .db import get_db

documents_bp = Blueprint("documents", __name__)


def _refuse(action, target=None):
    log_audit(get_db(), g.user["username"], g.user["role"], action, target, allowed=0)
    flash("Only a dentist can work with clinical documents.", "danger")
    return redirect(url_for("dashboard.index"))


def _patient(cf):
    if not is_valid_cf(cf):
        abort(404)
    conn = get_db()
    pid = patient_id.resolve(conn, cf)
    patient = lookup_patient(pid, conn) if pid else None
    if patient is None:
        abort(404)
    return conn, pid, patient


def _who():
    return g.user["username"], g.user["role"]


@documents_bp.route("/patients/<cf>/documents")
def index(cf):
    if not authorize(g.user["role"], docs.CAPABILITY):
        return _refuse("document_read", cf)
    conn, pid, patient = _patient(cf)
    query = (request.args.get("q") or "").strip()[:200]
    results, error = [], None
    if query:
        try:
            results = docs.search(conn, pid, query, *_who())
        except docs.DocumentError as e:
            error = str(e)
    return render_template("documents.html", cf=cf, patient=patient, rows=docs.for_patient(conn, pid),
                           query=query, results=results, error=error)


@documents_bp.route("/patients/<cf>/documents", methods=["POST"])
def upload(cf):
    if not authorize(g.user["role"], docs.CAPABILITY):
        return _refuse("document_upload", cf)
    conn, pid, _patient_row = _patient(cf)
    blob = request.files.get("file")
    if blob is None or not blob.filename:
        flash("Choose a file to upload.", "danger")
        return redirect(url_for("documents.index", cf=cf))
    data = blob.read(docs.MAX_BYTES + 1)
    doc_id = docs.ingest(conn, pid, data, blob.filename, *_who())
    status = docs.row(conn, doc_id)["status"]
    flash({"pending_review": "Read. Check the text and confirm it before it can be searched.",
           "quarantined": "Not accepted: the file was kept but not read.",
           "extraction_failed": "The file was kept, but it could not be read."}.get(status, "Saved."),
          "success" if status == "pending_review" else "danger")
    return redirect(url_for("documents.detail", cf=cf, did=doc_id))


@documents_bp.route("/patients/<cf>/documents/<int:did>")
def detail(cf, did):
    if not authorize(g.user["role"], docs.CAPABILITY):
        return _refuse("document_read", f"document:{did}")
    conn, pid, patient = _patient(cf)
    try:
        r = docs.load(conn, did, pid, *_who())
    except LookupError:
        abort(404)
    pages = json.loads(r["extraction"])["pages"] if r["extraction"] else []
    return render_template("document_detail.html", cf=cf, patient=patient, doc=r, pages=pages,
                           uncertain_below=docs.UNCERTAIN_BELOW)


def _step(cf, did, fn, done, **kwargs):
    conn, pid, _patient_row = _patient(cf)
    try:
        out = fn(conn, did, pid, actor=g.user["username"], role=g.user["role"], **kwargs)
    except LookupError:
        abort(404)
    except docs.DocumentError as e:
        flash(str(e), "danger")
        return redirect(url_for("documents.detail", cf=cf, did=did))
    flash(done, "success")
    return redirect(url_for("documents.detail", cf=cf, did=out if isinstance(out, int) else did))


@documents_bp.route("/patients/<cf>/documents/<int:did>/confirm", methods=["POST"])
def confirm(cf, did):
    if not authorize(g.user["role"], docs.CAPABILITY):
        return _refuse("document_confirm", f"document:{did}")
    return _step(cf, did, docs.confirm, "Confirmed. It is now part of the record and can be searched.")


@documents_bp.route("/patients/<cf>/documents/<int:did>/retry", methods=["POST"])
def retry(cf, did):
    if not authorize(g.user["role"], docs.CAPABILITY):
        return _refuse("document_retry", f"document:{did}")
    return _step(cf, did, docs.retry, "Read again. Check the result below.")


@documents_bp.route("/patients/<cf>/documents/<int:did>/reject", methods=["POST"])
def reject(cf, did):
    if not authorize(g.user["role"], docs.CAPABILITY):
        return _refuse("document_reject", f"document:{did}")
    return _step(cf, did, docs.reject, "Rejected. The file is kept; it is not part of the record.",
                 reason=request.form.get("reason", ""))


@documents_bp.route("/patients/<cf>/documents/<int:did>/replace", methods=["POST"])
def replace(cf, did):
    if not authorize(g.user["role"], docs.CAPABILITY):
        return _refuse("document_replace", f"document:{did}")
    blob = request.files.get("file")
    data = blob.read(docs.MAX_BYTES + 1) if blob else b""
    return _step(cf, did, docs.replace, "Replaced. The earlier version is kept in the history.",
                 data=data, name=blob.filename if blob else "")


@documents_bp.route("/patients/<cf>/documents/<int:did>/file")
def original(cf, did):
    if not authorize(g.user["role"], docs.CAPABILITY):
        return _refuse("document_read", f"document:{did}")
    conn, pid, _patient_row = _patient(cf)
    try:
        r = docs.load(conn, did, pid, *_who())
    except LookupError:
        abort(404)
    path = docs.original_path(r)
    if not path.exists():
        abort(404)
    # always a download, never rendered in the page: a PDF viewer or an image
    # decoder in the browser is not something a stored file gets to drive
    resp = send_file(str(path.resolve()), mimetype=docs.MIMETYPES.get(r["kind"], "application/octet-stream"),
                     as_attachment=True, download_name=r["display_name"])
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
    return resp
