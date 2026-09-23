from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

import patient_id
import visit_summary as vs
from auth import authorize, log_audit
from codice_fiscale import is_valid as is_valid_cf
from storage import lookup_patient

from .db import get_db

summary_bp = Blueprint("summary", __name__)


def _refuse(action, target=None):
    log_audit(get_db(), g.user["username"], g.user["role"], action, target, allowed=0)
    flash("Only a dentist can see or review a next-visit summary.", "danger")
    return redirect(url_for("dashboard.index"))


def _patient(cf):
    """The patient behind the URL, or 404. Checked before anything is read."""
    if not is_valid_cf(cf):
        abort(404)
    conn = get_db()
    pid = patient_id.resolve(conn, cf)
    if pid is None:
        abort(404)
    patient = lookup_patient(pid, conn)
    if patient is None:
        abort(404)
    return conn, pid, patient


def _back(cf):
    return redirect(url_for("summary.page", cf=cf))


def _act(cf, sid, fn, done_message, **kwargs):
    """Run one workflow step on this patient's summary. Another patient's id
    is not found; a refused step says why and changes nothing."""
    conn, pid, _patient_row = _patient(cf)
    try:
        fn(conn, sid, pid, actor=g.user["username"], role=g.user["role"], **kwargs)
    except LookupError:
        abort(404)
    except vs.SummaryError as e:
        flash(str(e), "danger")
        return _back(cf)
    flash(done_message, "success")
    return _back(cf)


@summary_bp.route("/patients/<cf>/summary")
def page(cf):
    if not authorize(g.user["role"], vs.CAPABILITY):
        return _refuse("summary_read", cf)
    conn, pid, patient = _patient(cf)
    rows = vs.for_patient(conn, pid)
    current = next((r for r in rows if r["status"] in ("draft", "approved")), None)
    view = vs.load(conn, current["id"], pid, g.user["username"], g.user["role"]) \
        if current else None
    return render_template("summary.html", cf=cf, patient=patient, view=view, history=rows,
                           label=vs.DRAFT_LABEL,
                           edit_text=vs.render_text(view["versions"][-1]["lines"]) if view else "")


@summary_bp.route("/patients/<cf>/summary/generate", methods=["POST"])
def generate(cf):
    if not authorize(g.user["role"], vs.CAPABILITY):
        return _refuse("summary_generate", cf)
    conn, pid, _patient_row = _patient(cf)
    try:
        vs.generate(conn, pid, g.user["username"], g.user["role"])
    except vs.SummaryError as e:
        flash(str(e), "danger")
    return _back(cf)


@summary_bp.route("/patients/<cf>/summary/<int:sid>/regenerate", methods=["POST"])
def regenerate(cf, sid):
    if not authorize(g.user["role"], vs.CAPABILITY):
        return _refuse("summary_regenerate", f"summary:{sid}")
    return _act(cf, sid, vs.regenerate, "A new draft was made. The old one is kept.")


@summary_bp.route("/patients/<cf>/summary/<int:sid>/edit", methods=["POST"])
def edit(cf, sid):
    if not authorize(g.user["role"], vs.CAPABILITY):
        return _refuse("summary_edit", f"summary:{sid}")
    return _act(cf, sid, vs.edit, "Saved as a new version.", text=request.form.get("body", ""))


@summary_bp.route("/patients/<cf>/summary/<int:sid>/approve", methods=["POST"])
def approve(cf, sid):
    if not authorize(g.user["role"], vs.CAPABILITY):
        return _refuse("summary_approve", f"summary:{sid}")
    return _act(cf, sid, vs.approve, "Approved.")


@summary_bp.route("/patients/<cf>/summary/<int:sid>/reject", methods=["POST"])
def reject(cf, sid):
    if not authorize(g.user["role"], vs.CAPABILITY):
        return _refuse("summary_reject", f"summary:{sid}")
    return _act(cf, sid, vs.reject, "Rejected. It stays in the history.",
                reason=request.form.get("reason", ""))
