import calendar
from datetime import date

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

import appointments
from auth import authorize, log_audit

from .db import get_db

appointments_bp = Blueprint("appointments", __name__)

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _denied(action, target=None):
    log_audit(get_db(), g.user["username"], g.user["role"], action, target, allowed=0)


def _allowed(action, target):
    log_audit(get_db(), g.user["username"], g.user["role"], action, target, allowed=1)


def _may():
    return authorize(g.user["role"], "manage_appointments")


def _pick_day(raw):
    # a hand-typed ?day= is user input, and it now drives which month is drawn
    # as well as which agenda is read. before the calendar a bad value just
    # returned an empty day; now it would reach date arithmetic, so it falls
    # back to today rather than raising.
    try:
        return date.fromisoformat(raw)
    except (TypeError, ValueError):
        return date.today()


def _pick_month(raw, day):
    # ?month=YYYY-MM, defaulting to the month the selected day is in
    try:
        year, month = raw.split("-")
        return date(int(year), int(month), 1)
    except (AttributeError, TypeError, ValueError):
        return day.replace(day=1)


def _shift_month(first, step):
    # first day of the month `step` months away. going through the month number
    # rather than adding days is what keeps december -> january on the right
    # year and never lands on the 31st of a 30-day month.
    month = first.month + step
    year = first.year + (month - 1) // 12
    return date(year, (month - 1) % 12 + 1, 1)


def _day_label(d, booked, requested):
    # what a screen reader gets instead of a bare number in a grid
    parts = [d.strftime("%-d %B %Y")]
    counts = []
    if booked:
        counts.append(f"{booked} booked")
    if requested:
        counts.append(f"{requested} request" if requested == 1 else f"{requested} requests")
    if counts:
        parts.append(", ".join(counts))
    else:
        parts.append("nothing booked")
    return ": ".join(parts)


def _month_grid(conn, first, selected):
    """The weeks of one month, each day carrying everything the template shows.

    Built here rather than in Jinja: the template would otherwise be doing date
    arithmetic and dictionary lookups per cell, and the counts have to line up
    with the aria-label on the same cell.
    """
    counts = appointments.month_counts(
        conn, first.isoformat(), _shift_month(first, 1).isoformat()
    )
    today = date.today()
    weeks = []
    for week in calendar.Calendar(firstweekday=0).monthdatescalendar(first.year, first.month):
        cells = []
        for d in week:
            day_counts = counts.get(d.isoformat(), {})
            booked = day_counts.get(appointments.BOOKED, 0)
            requested = day_counts.get(appointments.REQUESTED, 0)
            cells.append({
                "iso": d.isoformat(),
                "number": d.day,
                "in_month": d.month == first.month,
                "is_today": d == today,
                "is_selected": d == selected,
                "booked": booked,
                "requested": requested,
                "label": _day_label(d, booked, requested),
            })
        weeks.append(cells)
    return weeks


@appointments_bp.route("/appointments")
def index():
    # the gate decides whether the query RUNS, so a role without the capability
    # gets a response with no appointment in it - not a hidden one. same shape
    # as reports_routes.index and dashboard_routes.index (D-09/D-10, RBAC-03).
    if not _may():
        _denied("manage_appointments")
        return redirect(url_for("dashboard.index"))

    selected = _pick_day(request.args.get("day"))
    day = selected.isoformat()
    first = _pick_month(request.args.get("month"), selected)
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
        weeks=_month_grid(conn, first, selected),
        weekdays=WEEKDAYS,
        month=first.isoformat()[:7],
        month_label=first.strftime("%B %Y"),
        prev_month=_shift_month(first, -1).isoformat()[:7],
        next_month=_shift_month(first, 1).isoformat()[:7],
        today=date.today().isoformat(),
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
