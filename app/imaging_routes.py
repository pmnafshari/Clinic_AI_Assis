"""Demo imaging requests (P26): the dentist's request and reception's handoff, on the patient record."""
from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for

import clinic_guides
import imaging_requests as ir
import patient_id
from auth import authorize, log_audit
from codice_fiscale import is_valid as is_valid_cf
from storage import lookup_patient

from .db import get_db

imaging_bp = Blueprint("imaging", __name__)


def _who():
    return g.user["username"], g.user["role"]


def _refuse(action, target):
    log_audit(get_db(), g.user["username"], g.user["role"], action, target, allowed=0)
    flash("Only a dentist can create or change an imaging request.", "danger")
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


def _back(cf, rid=None):
    if rid:
        return redirect(url_for("imaging.view", cf=cf, rid=rid))
    return redirect(url_for("patients.detail_view", cf=cf) + "#imaging")


def _step(cf, rid, fn, done, **kwargs):
    conn, pid, _p = _patient(cf)
    try:
        fn(conn, rid, pid, actor=g.user["username"], role=g.user["role"], **kwargs)
    except LookupError:
        abort(404)
    except PermissionError:
        return _refuse("imaging_change", f"imaging:{rid}")
    except (ir.RequestError, ValueError) as e:
        flash(str(e), "danger")
        return _back(cf, rid)
    flash(done, "success")
    return _back(cf, rid)


@imaging_bp.route("/patients/<cf>/imaging", methods=["POST"])
def create(cf):
    if not authorize(g.user["role"], ir.MANAGE):
        return _refuse("imaging_create", cf)
    conn, pid, _p = _patient(cf)
    f = request.form
    try:
        rid, warnings = ir.create_draft(conn, pid, f.get("exam", ""), f.get("label", ""), f.get("note", ""), *_who(),
                                        token=f.get("submit_token", ""))
    except ir.RequestError as e:
        flash(str(e), "danger")
        return _back(cf)
    for w in warnings:
        flash(w, "warning")
    flash("Draft saved. Nothing happens until you activate it.", "success")
    return _back(cf, rid)


@imaging_bp.route("/patients/<cf>/imaging/<int:rid>")
def view(cf, rid):
    if not authorize(g.user["role"], ir.VIEW):
        return _refuse("imaging_read", f"imaging:{rid}")
    conn, pid, patient = _patient(cf)
    try:
        h = ir.handoff(conn, pid, rid, *_who())
    except LookupError:
        abort(404)
    r = h["request"]
    manage = authorize(g.user["role"], ir.MANAGE)
    guide = None
    if h["current"]:
        gconn = clinic_guides.connect()
        try:
            guide = ir.guidance(gconn, r["exam"], g.user["role"])
        finally:
            gconn.close()
    import secrets
    resp = Response(render_template(
        "imaging_request.html", cf=cf, patient=patient, h=h, r=r, guide=guide, manage=manage,
        history=ir.history(conn, rid, pid, *_who()), exams=ir.EXAMS,
        files=ir.linked_files(conn, rid, pid, *_who()) if manage else [],
        linkable=ir.linkable_files(conn, pid) if manage and h["current"] else [],
        token=secrets.token_urlsafe(16)))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@imaging_bp.route("/patients/<cf>/imaging/<int:rid>/files")
def files(cf, rid):
    if not authorize(g.user["role"], ir.MANAGE):
        return _refuse("imaging_read", f"imaging:{rid}")
    conn, pid, _p = _patient(cf)
    try:
        ir.handoff(conn, pid, rid, *_who())
    except LookupError:
        abort(404)
    return redirect(url_for("imaging.view", cf=cf, rid=rid) + "#files")


@imaging_bp.route("/patients/<cf>/imaging/<int:rid>/activate", methods=["POST"])
def activate(cf, rid):
    if not authorize(g.user["role"], ir.MANAGE):
        return _refuse("imaging_activate", f"imaging:{rid}")
    return _step(cf, rid, ir.activate, "Active. Reception can now see it.",
                 expected_version=request.form.get("version", "0"))


@imaging_bp.route("/patients/<cf>/imaging/<int:rid>/cancel", methods=["POST"])
def cancel(cf, rid):
    if not authorize(g.user["role"], ir.MANAGE):
        return _refuse("imaging_cancel", f"imaging:{rid}")
    return _step(cf, rid, ir.cancel, "Cancelled. Reception sees that it was cancelled.",
                 expected_version=request.form.get("version", "0"), reason=request.form.get("reason", ""))


@imaging_bp.route("/patients/<cf>/imaging/<int:rid>/revise", methods=["POST"])
def revise(cf, rid):
    if not authorize(g.user["role"], ir.MANAGE):
        return _refuse("imaging_revise", f"imaging:{rid}")
    conn, pid, _p = _patient(cf)
    f = request.form
    try:
        new, warnings = ir.revise(conn, rid, pid, f.get("exam", ""), f.get("label", ""), f.get("note", ""), *_who(),
                                  token=f.get("submit_token", ""))
    except LookupError:
        abort(404)
    except ir.RequestError as e:
        flash(str(e), "danger")
        return _back(cf, rid)
    flash("New version saved as a draft. The active version stays until you activate this one.", "success")
    return _back(cf, new)


@imaging_bp.route("/patients/<cf>/imaging/<int:rid>/acknowledge", methods=["POST"])
def acknowledge(cf, rid):
    if not authorize(g.user["role"], ir.VIEW):
        return _refuse("imaging_acknowledge", f"imaging:{rid}")
    return _step(cf, rid, ir.acknowledge, "Noted as seen.", expected_version=request.form.get("version", "0"))


@imaging_bp.route("/patients/<cf>/imaging/<int:rid>/ask", methods=["POST"])
def ask(cf, rid):
    if not authorize(g.user["role"], ir.VIEW):
        return _refuse("imaging_question", f"imaging:{rid}")
    return _step(cf, rid, ir.ask_dentist, "The dentist will see your question on this request. No message is sent.")


@imaging_bp.route("/patients/<cf>/imaging/<int:rid>/link", methods=["POST"])
def link(cf, rid):
    if not authorize(g.user["role"], ir.MANAGE):
        return _refuse("imaging_link", f"imaging:{rid}")
    raw = request.form.get("document_id", "")
    return _step(cf, rid, ir.link_file, "Linked. The file is not published and is not marked as read or performed.",
                 document_id=int(raw) if raw.isdigit() else 0, verified=request.form.get("verified") == "1")
