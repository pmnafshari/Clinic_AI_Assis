from datetime import date

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

import appointments
from auth import authorize, log_audit

from .db import get_db

appointments_bp = Blueprint("appointments", __name__)


def _denied(action, target=None):
    log_audit(get_db(), g.user["username"], g.user["role"], action, target, allowed=0)


def _allowed(action, target):
    log_audit(get_db(), g.user["username"], g.user["role"], action, target, allowed=1)


def _may():
    return authorize(g.user["role"], "manage_appointments")


@appointments_bp.route("/appointments")
def index():
    # the gate decides whether the query RUNS, so a role without the capability
    # gets a response with no appointment in it - not a hidden one. same shape
    # as reports_routes.index and dashboard_routes.index (D-09/D-10, RBAC-03).
    if not _may():
        _denied("manage_appointments")
        return redirect(url_for("dashboard.index"))

    day = request.args.get("day") or date.today().isoformat()
    conn = get_db()
    rows = appointments.agenda(conn, day)
    # patient requests waiting for a slot. read behind the same gate as the
    # agenda, so a role without the capability never runs this query either.
    requests_pending = appointments.pending_requests(conn)
    patients = conn.execute(
        "SELECT codice_fiscale, patient_name FROM patients ORDER BY patient_name"
    ).fetchall()
    dentists = conn.execute(
        "SELECT username FROM users WHERE role = 'dentist' AND active = 1 ORDER BY username"
    ).fetchall()
    return render_template(
        "appointments.html",
        day=day,
        rows=rows,
        has_data=bool(rows),
        patients=patients,
        dentists=dentists,
        requests_pending=requests_pending,
    )


@appointments_bp.route("/appointments/book", methods=["POST"])
def book():
    if not _may():
        _denied("manage_appointments")
        return redirect(url_for("dashboard.index"))

    day = request.form.get("day") or date.today().isoformat()
    starts_at = f"{request.form.get('date', '')}T{request.form.get('time', '')}"
    try:
        new_id = appointments.book(
            get_db(),
            request.form.get("codice_fiscale", ""),
            request.form.get("dentist", ""),
            starts_at,
            request.form.get("minutes", ""),
            request.form.get("note") or None,
        )
    except ValueError as e:
        # an overlap is a user mistake, not a server error
        flash(str(e), "error")
    else:
        _allowed("book_appointment", str(new_id))
        flash("Appointment booked.", "success")
    return redirect(url_for("appointments.index", day=day))


@appointments_bp.route("/appointments/<int:appointment_id>/cancel", methods=["POST"])
def cancel(appointment_id):
    if not _may():
        _denied("manage_appointments", str(appointment_id))
        return redirect(url_for("dashboard.index"))

    day = request.form.get("day") or date.today().isoformat()
    try:
        appointments.cancel(get_db(), appointment_id)
    except ValueError as e:
        flash(str(e), "error")
    else:
        _allowed("cancel_appointment", str(appointment_id))
        flash("Appointment cancelled.", "success")
    return redirect(url_for("appointments.index", day=day))


@appointments_bp.route("/appointments/<int:appointment_id>/reschedule", methods=["POST"])
def reschedule(appointment_id):
    if not _may():
        _denied("manage_appointments", str(appointment_id))
        return redirect(url_for("dashboard.index"))

    day = request.form.get("day") or date.today().isoformat()
    starts_at = f"{request.form.get('date', '')}T{request.form.get('time', '')}"
    try:
        appointments.reschedule(
            get_db(), appointment_id, starts_at, request.form.get("minutes", "")
        )
    except ValueError as e:
        flash(str(e), "error")
    else:
        _allowed("reschedule_appointment", str(appointment_id))
        flash("Appointment moved.", "success")
        day = request.form.get("date") or day
    return redirect(url_for("appointments.index", day=day))


# --- patient requests (Phase 42) -------------------------------------------
#
# A request is not on the calendar: no dentist, no duration, and only the date
# part of starts_at means anything. Confirming is what assigns a real slot, and
# it goes through appointments.confirm(), which shares book()'s overlap rule -
# so a confirm that double-books is refused exactly as a booking is.

@appointments_bp.route("/appointments/<int:appointment_id>/confirm", methods=["POST"])
def confirm(appointment_id):
    if not _may():
        _denied("manage_appointments", str(appointment_id))
        return redirect(url_for("dashboard.index"))

    day = request.form.get("day") or date.today().isoformat()
    starts_at = f"{request.form.get('date', '')}T{request.form.get('time', '')}"
    try:
        appointments.confirm(
            get_db(), appointment_id, request.form.get("dentist", ""),
            starts_at, request.form.get("minutes", ""),
        )
    except ValueError as e:
        flash(str(e), "error")
    else:
        _allowed("confirm_appointment_request", str(appointment_id))
        flash("Request confirmed.", "success")
    return redirect(url_for("appointments.index", day=day))


@appointments_bp.route("/appointments/<int:appointment_id>/decline", methods=["POST"])
def decline(appointment_id):
    if not _may():
        _denied("manage_appointments", str(appointment_id))
        return redirect(url_for("dashboard.index"))

    day = request.form.get("day") or date.today().isoformat()
    try:
        appointments.decline(get_db(), appointment_id,
                             (request.form.get("reason") or "").strip() or None)
    except ValueError as e:
        flash(str(e), "error")
    else:
        _allowed("decline_appointment_request", str(appointment_id))
        flash("Request declined.", "success")
    return redirect(url_for("appointments.index", day=day))
