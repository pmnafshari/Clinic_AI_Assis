from pathlib import Path

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

import ask
from auth import authorize, log_audit
from dental_notes_schema import CF_PATTERN
from storage import lookup_clinical, lookup_patient

from .db import get_db

patients_bp = Blueprint("patients", __name__)

SORTED_ROOT = Path("sorted")


@patients_bp.route("/patients")
def list_view():
    if not authorize(g.user["role"], "read_notes"):
        log_audit(get_db(), g.user["username"], g.user["role"], "read_notes", None, allowed=0)
        flash("You don't have permission to view patient records.")
        return redirect(url_for("dashboard.index"))

    patients = get_db().execute("""
        SELECT p.codice_fiscale, p.patient_name, p.phone,
            (SELECT next_appointment FROM visits v WHERE v.codice_fiscale = p.codice_fiscale
             ORDER BY v.id DESC LIMIT 1) AS next_appointment,
            (SELECT visit_date FROM visits v WHERE v.codice_fiscale = p.codice_fiscale
             ORDER BY v.id DESC LIMIT 1) AS last_visit
        FROM patients p
        ORDER BY p.patient_name
    """).fetchall()
    return render_template("patients_list.html", patients=patients)


@patients_bp.route("/patients/search")
def search_fragment():
    # HTMX target - a denied fragment returns a bare status, not a redirect
    if not authorize(g.user["role"], "read_notes"):
        return "", 403

    query = request.args.get("q", "")
    candidates = ask.fuzzy_lookup(query, get_db())
    return render_template("_patient_candidates.html", candidates=candidates, query=query)


@patients_bp.route("/patients/<cf>")
def detail_view(cf):
    if not authorize(g.user["role"], "read_notes"):
        log_audit(get_db(), g.user["username"], g.user["role"], "read_notes", cf, allowed=0)
        flash("You don't have permission to view patient records.")
        return redirect(url_for("dashboard.index"))

    # validate before any db/filesystem access - cf is a raw path segment
    if not CF_PATTERN.match(cf):
        abort(404)

    conn = get_db()
    patient = lookup_patient(cf, conn)
    if patient is None:
        abort(404)

    # dentist-only gate for the clinical card - never read_notes, which
    # assistant also holds (RBAC-03)
    show_clinical = authorize(g.user["role"], "read_clinical")
    clinical = lookup_clinical(cf, conn) if show_clinical else None

    patient_dir = SORTED_ROOT / cf
    files = []
    if patient_dir.is_dir():
        files = sorted(str(f.relative_to(SORTED_ROOT)) for f in patient_dir.rglob("*") if f.is_file())

    return render_template(
        "patients_detail.html",
        cf=cf,
        patient=patient,
        clinical=clinical,
        show_clinical=show_clinical,
        files=files,
    )
