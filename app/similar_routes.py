from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

import patient_id
import similar_cases as sc
from auth import authorize, log_audit
from codice_fiscale import is_valid as is_valid_cf
from storage import lookup_patient

from .db import get_db

similar_bp = Blueprint("similar", __name__)


def _refuse(action, target):
    log_audit(get_db(), g.user["username"], g.user["role"], action, target, allowed=0)
    flash("Only a dentist can look at similar cases.", "danger")
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


@similar_bp.route("/patients/<cf>/visits/<int:vid>/similar")
def page(cf, vid):
    if not authorize(g.user["role"], sc.CAPABILITY):
        return _refuse("similar_cases", f"visit:{vid}")
    conn, pid, patient = _patient(cf)
    view = "teaching" if request.args.get("view") == "teaching" else "minimised"
    try:
        found = sc.find(conn, vid, pid, g.user["username"], g.user["role"], view=view)
    except LookupError:
        abort(404)
    return render_template("similar_cases.html", cf=cf, patient=patient, vid=vid, found=found)


@similar_bp.route("/patients/<cf>/visits/<int:vid>/similar/<int:case>/feedback", methods=["POST"])
def feedback(cf, vid, case):
    if not authorize(g.user["role"], sc.CAPABILITY):
        return _refuse("similar_case_feedback", f"visit:{vid}")
    conn, pid, _patient_row = _patient(cf)
    try:
        sc.feedback(conn, vid, pid, case, request.form.get("verdict", ""),
                    request.form.get("reason", ""), g.user["username"], g.user["role"])
    except LookupError:
        abort(404)
    except ValueError as e:
        flash(str(e), "danger")
        return redirect(url_for("similar.page", cf=cf, vid=vid))
    flash("Recorded. Your verdict does not change any record or the list.", "success")
    return redirect(url_for("similar.page", cf=cf, vid=vid))


@similar_bp.route("/patients/<cf>/visits/<int:vid>/similar/<int:case>/exclude", methods=["POST"])
def exclude(cf, vid, case):
    if not authorize(g.user["role"], sc.CAPABILITY):
        return _refuse("similar_case_exclude", f"visit:{case}")
    conn, pid, _patient_row = _patient(cf)
    try:
        sc.source(conn, vid, pid)  # the source must be this patient's visit too
        sc.exclude(conn, case, request.form.get("reason", ""), g.user["username"], g.user["role"],
                   pid=pid)
    except LookupError:
        abort(404)
    flash("Removed from similar-case search. The visit itself is unchanged.", "success")
    return redirect(url_for("similar.page", cf=cf, vid=vid))
