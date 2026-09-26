import calendar
from datetime import date, timedelta

import clinic_time

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
        return clinic_time.now().date()     # the clinic's today (P23, F1)


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


def _day_label(d, booked, requested, word="booked"):
    # what a screen reader gets instead of a bare number in a grid
    parts = [d.strftime("%-d %B %Y")]
    counts = []
    if booked:
        counts.append(f"{booked} {word}")
    if requested:
        counts.append(f"{requested} request" if requested == 1 else f"{requested} requests")
    if counts:
        parts.append(", ".join(counts))
    else:
        parts.append(f"nothing {word}")
    return ": ".join(parts)


def _month_grid(conn, first, selected, per_day, word):
    """The weeks of one month, each day carrying everything the template shows. `per_day` is the view's own
    range_rows grouped by clinic day - the same rows Day and Week draw - so a month count cannot disagree with them.
    requests come from month_counts and stay a separate mark: a preferred day, never a booking."""
    requested_by_day = appointments.month_counts(conn, first.isoformat(), _shift_month(first, 1).isoformat())
    today = clinic_time.now().date()
    weeks = []
    for week in calendar.Calendar(firstweekday=0).monthdatescalendar(first.year, first.month):
        cells = []
        for d in week:
            booked = len(per_day.get(d.isoformat(), [])) if d.month == first.month else 0
            requested = requested_by_day.get(d.isoformat(), {}).get(appointments.REQUESTED, 0)
            cells.append({
                "iso": d.isoformat(),
                "number": d.day,
                "in_month": d.month == first.month,
                "is_today": d == today,
                "is_selected": d == selected,
                "booked": booked,
                "requested": requested,
                "label": _day_label(d, booked, requested, word),
            })
        weeks.append(cells)
    return weeks


VIEWS = ("day", "week", "month")
STATUS_FILTERS = {"booked": (appointments.BOOKED,), "cancelled": ("cancelled",), "all": (appointments.BOOKED, "cancelled")}
# what the summary and the month cells call the rows each status filter shows
STATUS_WORDS = {"booked": ("confirmed booking", "confirmed bookings", "booked"),
                "cancelled": ("cancelled appointment", "cancelled appointments", "cancelled"),
                "all": ("booked or cancelled appointment", "booked or cancelled appointments", "booked or cancelled")}


def _slot(row):
    start = clinic_time.local_of(row["starts_at"])
    end = start + timedelta(minutes=row["minutes"])
    return {"id": row["id"], "patient_name": row["patient_name"], "dentist": row["dentist"], "status": row["status"],
            "hour": start.hour, "start": start.strftime("%H:%M"), "end": end.strftime("%H:%M"),
            "minutes": row["minutes"], "note": row["note"], "starts_at": row["starts_at"]}


@appointments_bp.route("/appointments")
def index():
    if not _may():
        _denied("manage_appointments")
        return redirect(url_for("dashboard.index"))

    selected = _pick_day(request.args.get("day"))
    day = selected.isoformat()
    view = request.args.get("view") or ("month" if request.args.get("month") else "day")
    if view not in VIEWS:
        view = "day"
    status = request.args.get("status") if request.args.get("status") in STATUS_FILTERS else "booked"
    conn = get_db()
    dentists = [r["username"] for r in conn.execute(
        "SELECT username FROM users WHERE role = 'dentist' AND active = 1 ORDER BY username")]
    dentist = request.args.get("dentist") if request.args.get("dentist") in dentists else None
    statuses = STATUS_FILTERS[status]
    one, many, word = STATUS_WORDS[status]
    first = _pick_month(request.args.get("month"), selected)
    monday = selected - timedelta(days=selected.weekday())

    # ONE dataset per view: the same range_rows, the same filters, grouped by clinic day
    if view == "day":
        lo, hi = selected, selected + timedelta(days=1)
    elif view == "week":
        lo, hi = monday, monday + timedelta(days=7)
    else:
        lo, hi = first, _shift_month(first, 1)
    per_day = {}
    for r in appointments.range_rows(conn, lo.isoformat(), hi.isoformat(), statuses, dentist):
        per_day.setdefault(clinic_time.local_date(r["starts_at"]), []).append(_slot(r))
    total = sum(len(v) for v in per_day.values())
    period = {"day": f"on {selected:%a %-d %b}", "week": "this week", "month": f"in {first:%B %Y}"}[view]
    summary = f"{total} {one if total == 1 else many} {period} · {dentist or 'all clinicians'}"

    # an empty view sends the user to the next CONFIRMED booking after it (with the clinician filter)
    upcoming = None
    if not total:
        nxt = appointments.next_confirmed(conn, (hi - timedelta(days=1)).isoformat(), dentist)
        if nxt:
            at = clinic_time.local_of(nxt["starts_at"])
            upcoming = {"iso": at.date().isoformat(), "label": f"{at:%a %-d %b}, {at:%H:%M}", "patient": nxt["patient_name"]}

    columns = [dentist] if dentist else dentists
    slots = per_day.get(day, []) if view == "day" else []
    grid = []
    if slots:
        # only the hours that hold something - no screen of empty rows
        hours = range(min(s["hour"] for s in slots), max(s["hour"] for s in slots) + 1)
        grid = [{"hour": f"{h:02d}:00", "cells": [[s for s in slots if s["hour"] == h and s["dentist"] == c] for c in columns]}
                for h in hours]
    week = [{"iso": (monday + timedelta(days=i)).isoformat(), "label": f"{(monday + timedelta(days=i)):%a %-d %b}",
             "name": f"{(monday + timedelta(days=i)):%a}", "date": f"{(monday + timedelta(days=i)):%-d %b}",
             "is_today": monday + timedelta(days=i) == clinic_time.now().date(),
             "slots": per_day.get((monday + timedelta(days=i)).isoformat(), [])}
            for i in range(7)] if view == "week" else None
    if view == "month":
        prev_day, next_day = _shift_keep_day(selected, -1), _shift_keep_day(selected, 1)
    else:
        step = timedelta(days=1 if view == "day" else 7)
        prev_day, next_day = (selected - step).isoformat(), (selected + step).isoformat()
    patients = conn.execute(
        "SELECT patient_id, codice_fiscale, patient_name FROM patients ORDER BY patient_name").fetchall()
    return render_template(
        "appointments.html",
        view=view, day=day, day_label=f"{selected:%a %-d %b %Y}", status=status, dentist=dentist,
        dentists=[{"username": d} for d in dentists], dentist_names=dentists, columns=columns, grid=grid,
        slot_count=len(slots), week=week, week_label=f"{monday:%-d %b} - {(monday + timedelta(days=6)):%-d %b %Y}",
        summary=summary, upcoming=upcoming, word=word, total=total,
        prev_day=prev_day, next_day=next_day,
        requests_pending=appointments.pending_requests(conn), patients=patients,
        open_booking=request.args.get("book") == "1",
        weeks=_month_grid(conn, first, selected, per_day, word) if view == "month" else [], weekdays=WEEKDAYS,
        month=first.isoformat()[:7], month_label=first.strftime("%B %Y"),
        today=clinic_time.now().date().isoformat(),
    )


def _shift_keep_day(d, step):
    # the same day-of-month one month away, clamped to that month's length - so Month -> Day keeps a real date
    first = _shift_month(d.replace(day=1), step)
    last = calendar.monthrange(first.year, first.month)[1]
    return first.replace(day=min(d.day, last)).isoformat()


@appointments_bp.route("/appointments/book", methods=["POST"])
def book():
    if not _may():
        _denied("manage_appointments")
        return redirect(url_for("dashboard.index"))

    day = request.form.get("day") or clinic_time.now().date().isoformat()
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

    day = request.form.get("day") or clinic_time.now().date().isoformat()
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

    day = request.form.get("day") or clinic_time.now().date().isoformat()
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

    day = request.form.get("day") or clinic_time.now().date().isoformat()
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

    day = request.form.get("day") or clinic_time.now().date().isoformat()
    try:
        appointments.decline(get_db(), appointment_id,
                             (request.form.get("reason") or "").strip() or None)
    except ValueError as e:
        flash(str(e), "error")
    else:
        _allowed("decline_appointment_request", str(appointment_id))
        flash("Request declined.", "success")
    return redirect(url_for("appointments.index", day=day))
