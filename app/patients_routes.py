from pathlib import Path

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

import sqlite3

import agent
import ask
import patient_auth
import pending_actions
from auth import authorize, log_audit
from dental_notes_schema import CF_PATTERN
from storage import lookup_clinical, lookup_patient

from .db import get_db

patients_bp = Blueprint("patients", __name__)

SORTED_ROOT = Path("sorted")

# newest by visit_date, not by insert order - a bulk load_from_sorted re-import
# assigns visit ids by filename, so the highest id is not the latest visit.
# same ordering as agent.pick_target_visit; nulls sort last.
LATEST_VISIT = "ORDER BY v.visit_date IS NULL, v.visit_date DESC, v.id DESC LIMIT 1"


@patients_bp.route("/patients")
def list_view():
    if not authorize(g.user["role"], "read_notes"):
        log_audit(get_db(), g.user["username"], g.user["role"], "read_notes", None, allowed=0)
        flash("You don't have permission to view patient records.")
        return redirect(url_for("dashboard.index"))

    patients = get_db().execute(f"""
        SELECT p.codice_fiscale, p.patient_name, p.phone,
            (SELECT next_appointment FROM visits v WHERE v.codice_fiscale = p.codice_fiscale
             {LATEST_VISIT}) AS next_appointment,
            (SELECT visit_date FROM visits v WHERE v.codice_fiscale = p.codice_fiscale
             {LATEST_VISIT}) AS last_visit
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

    return render_template(
        "patients_detail.html",
        cf=cf,
        patient=patient,
        clinical=clinical,
        show_clinical=show_clinical,
    )


@patients_bp.route("/patients/<cf>/files")
def files_fragment(cf):
    # HTMX-polled fragment - a denied fragment returns a bare status, never
    # a redirect (an HX-swap would otherwise get a whole login/dashboard page
    # every 5 seconds). filenames are clinical data too (notes/, images/,
    # records/), so this stays read_clinical, not read_notes (CR-01/RBAC-03)
    if not authorize(g.user["role"], "read_clinical"):
        return "", 403

    if not CF_PATTERN.match(cf):
        abort(404)

    patient_dir = SORTED_ROOT / cf
    files = []
    if patient_dir.is_dir():
        files = sorted(
            str(f.relative_to(SORTED_ROOT)) for f in patient_dir.rglob("*") if f.is_file()
        )
    return render_template("_patient_files.html", files=files)


@patients_bp.route("/patients/<cf>/issue-pin", methods=["POST"])
def issue_pin_submit(cf):
    # HTMX target - a denied fragment returns a bare status, not a redirect.
    # there is deliberately no GET counterpart: only the hash is stored, so a
    # pin cannot be re-displayed even if someone wanted to build one.
    if not authorize(g.user["role"], "issue_patient_pin"):
        log_audit(get_db(), g.user["username"], g.user["role"], "issue_patient_pin",
                  cf, allowed=0)
        return "", 403

    if not CF_PATTERN.match(cf):
        abort(404)

    conn = get_db()
    if lookup_patient(cf, conn) is None:
        abort(404)

    try:
        pin = patient_auth.issue_pin(cf, conn, g.user["username"], g.user["role"])
    except sqlite3.IntegrityError:
        # the patient row went away between the lookup and the write
        abort(404)

    # the plaintext goes into this one response body and nowhere else: not a
    # flash (which survives a redirect), not the url, not the log, not the
    # audit row. issue_pin already recorded that an issuance happened.
    return render_template("_patient_pin.html", cf=cf, pin=pin)


@patients_bp.route("/patients/<cf>/edit-form")
def edit_form_fragment(cf):
    # HTMX target - a denied fragment returns a bare status, not a redirect
    if not authorize(g.user["role"], "read_notes"):
        return "", 403

    if not CF_PATTERN.match(cf):
        abort(404)

    field = request.args.get("field", "")
    if field not in agent.EDITABLE_FIELDS:
        abort(400)

    patient = lookup_patient(cf, get_db())
    if patient is None:
        abort(404)

    value = patient[agent.EDITABLE_FIELDS[field]]
    return render_template("_edit_form.html", cf=cf, field=field, value=value)


@patients_bp.route("/patients/<cf>/edit", methods=["POST"])
def edit_submit(cf):
    if not authorize(g.user["role"], "read_notes"):
        return "", 403

    if not CF_PATTERN.match(cf):
        abort(404)

    field = request.form.get("field", "")
    value = request.form.get("value", "")

    conn = get_db()
    patient = lookup_patient(cf, conn)
    if patient is None:
        abort(404)

    # build_pending_action resolves by name, not cf - choose_cf pins the
    # resolution to the cf this route already validated, so a duplicate
    # name elsewhere can never redirect the edit to the wrong patient
    call = agent.ToolCall(
        tool="update_field",
        args={"patient": patient["patient_name"], "field": field, "value": value},
    )
    try:
        call.parsed_args()
    except Exception as e:
        return render_template(
            "_edit_form.html", cf=cf, field=field, value=value, error=f"invalid field: {e}"
        )

    pending, reason = agent.build_pending_action(
        call, conn, g.user["role"], g.user["username"],
        choose_cf=lambda candidates, cf=cf: cf if cf in candidates else None,
    )
    if pending is None:
        return render_template("_edit_form.html", cf=cf, field=field, value=value, error=reason)

    token = pending_actions.create_pending_action(conn, g.user["username"], g.user["role"], pending)
    return render_template("_confirm_diff.html", diff_line=pending["diff_line"], token=token)


@patients_bp.route("/patients/<cf>/visits/<int:visit_id>/edit-form")
def visit_edit_form_fragment(cf, visit_id):
    # HTMX target - a denied fragment returns a bare status, not a redirect.
    # gated on read_clinical (dentist-only) since this exposes clinical data
    # (visit date, next appointment), not on read_notes (RBAC-03 / CR-01)
    if not authorize(g.user["role"], "read_clinical"):
        return "", 403

    if not CF_PATTERN.match(cf):
        abort(404)

    row = get_db().execute(
        "SELECT visit_date, next_appointment FROM visits WHERE id = ? AND codice_fiscale = ?",
        (visit_id, cf),
    ).fetchone()
    if row is None:
        abort(404)

    return render_template(
        "_visit_edit_form.html", cf=cf, visit_id=visit_id,
        value=row["next_appointment"], visit_date=row["visit_date"],
    )


@patients_bp.route("/patients/<cf>/visits/<int:visit_id>/edit", methods=["POST"])
def visit_edit_submit(cf, visit_id):
    if not authorize(g.user["role"], "read_clinical"):
        return "", 403

    if not CF_PATTERN.match(cf):
        abort(404)

    value = request.form.get("value", "")

    conn = get_db()
    patient = lookup_patient(cf, conn)
    if patient is None:
        abort(404)

    call = agent.ToolCall(
        tool="update_visit_field",
        args={"patient": patient["patient_name"], "visit_id": visit_id, "value": value},
    )
    try:
        call.parsed_args()
    except Exception as e:
        return render_template(
            "_visit_edit_form.html", cf=cf, visit_id=visit_id, value=value,
            error=f"invalid value: {e}",
        )

    pending, reason = agent.build_pending_action(
        call, conn, g.user["role"], g.user["username"], sorted_root=SORTED_ROOT,
        choose_cf=lambda candidates, cf=cf: cf if cf in candidates else None,
    )
    if pending is None:
        return render_template(
            "_visit_edit_form.html", cf=cf, visit_id=visit_id, value=value, error=reason
        )

    token = pending_actions.create_pending_action(conn, g.user["username"], g.user["role"], pending)
    return render_template("_confirm_diff.html", diff_line=pending["diff_line"], token=token)
