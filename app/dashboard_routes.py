import json
from datetime import timedelta
from pathlib import Path

from flask import Blueprint, g, redirect, render_template, url_for

import agent
import appointments
import clinic_time
import inventory
from auth import authorize
from shared.names import initials, tint

from .db import get_db
from .upload_routes import _intake_state

dashboard_bp = Blueprint("dashboard", __name__)

# the same six states _intake_state returns, with the labels
# _recent_intake.html already prints. one list so the chart legend and the
# badges on the same screen cannot drift apart.
INTAKE_LABELS = [
    ("sorted", "Sorted"),
    ("needs_review", "Needs Review"),
    ("not_searchable", "Not searchable"),
    ("queued", "Queued"),
    ("external", "External"),
    ("rejected", "Rejected"),
]

# module-level, same as app/agent_routes.py's UNDO_LOG - a selftest patches
# dashboard_routes.UNDO_LOG, so it must be read fresh (log_path=None below),
# not frozen into a default argument at def time
UNDO_LOG = agent.UNDO_LOG


def _user_undo_history(username, log_path=None, limit=10):
    log_path = log_path or UNDO_LOG
    log_file = Path(log_path)
    if not log_file.exists():
        return []
    lines = log_file.read_text().strip().splitlines()
    if not lines:
        return []
    mine = []
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            # a truncated/hand-edited line must not 500 the whole dashboard
            continue
        if entry.get("username") == username:
            mine.append(entry)
    return mine[:limit]


def _agenda_view(rows, now):
    # everything the agenda card needs, shaped here so the template computes
    # nothing (D-01). past/current are decided against the server clock -
    # the same clock that decided which day "today" is.
    view = []
    for r in rows:
        # the CLINIC's wall time. starts_at is a UTC instant since P52, so
        # slicing it would print the UTC hour - an agenda an hour or two out
        # all summer, which is exactly the kind of wrong that looks fine.
        start = clinic_time.local_of(r["starts_at"])
        end_minutes = start.hour * 60 + start.minute + r["minutes"]
        now_minutes = now.hour * 60 + now.minute
        view.append({
            "time": start.strftime("%H:%M"),
            "end": f"{end_minutes // 60 % 24:02d}:{end_minutes % 60:02d}",
            "cf": r["cf"],
            "patient_name": r["patient_name"],
            "initials": initials(r["patient_name"]),
            "tint": tint(r["patient_name"]),
            "minutes": r["minutes"],
            "note": r["note"],
            "dentist": r["dentist"],
            "dentist_initials": initials(r["dentist"]),
            "dentist_tint": tint(r["dentist"]),
            "is_past": end_minutes <= now_minutes,
            "is_current": start.hour * 60 + start.minute <= now_minutes < end_minutes,
        })
    return view


def _request_view(rows, limit=5):
    # a request carries a date and a period, never a time (PAPT-05) - the
    # time part of starts_at is meaningless and is not copied out
    return [{
        "patient_name": r["patient_name"],
        "initials": initials(r["patient_name"]),
        "tint": tint(r["patient_name"]),
        "day": r["starts_at"][:10],
        "cf": r["cf"],
        "period": r["period"],
        "reason": r["note"],
    } for r in rows[:limit]]


def _intake_counts(conn, username):
    # per-user, with _user_recent_intake's scoping (D-19) rather than
    # clinic-wide: the recent-intake list sits on this same screen, and a
    # clinic-wide KPI saying "2 need review" above a personal list showing
    # none would read as a bug. role != 'system' keeps watcher and backfill
    # rows out structurally (D-11).
    rows = conn.execute(
        "SELECT target, action, allowed FROM audit_log"
        " WHERE username = ? AND action IN ('queue_upload', 'upload_file', 'sync_note')"
        " AND role != 'system' AND target IS NOT NULL"
        " ORDER BY id DESC",
        (username,),
    ).fetchall()

    # same collapse as _user_recent_intake: one row per filename, newest
    # first, so the three rows a .txt produces count once at its final state
    seen = set()
    counts = {state: 0 for state, _ in INTAKE_LABELS}
    for row in rows:
        name = Path(row["target"]).name
        if name in seen:
            continue
        seen.add(name)
        counts[_intake_state(row)] += 1
    return counts


def _visits_by_month(conn, months=12):
    # clinic-wide, because visits carry no user column - a per-user split
    # does not exist in the schema. counts only, never a visit row (D-07).
    rows = conn.execute(
        "SELECT substr(visit_date, 1, 7) AS month, COUNT(*) AS n FROM visits"
        " WHERE visit_date IS NOT NULL AND visit_date != ''"
        " GROUP BY month ORDER BY month DESC LIMIT ?",
        (months,),
    ).fetchall()
    rows = list(reversed(rows))
    return [r["month"] for r in rows], [r["n"] for r in rows]


@dashboard_bp.route("/")
def index():
    # someone who manages users and has no clinical access gets their own
    # landing page - the clinical dashboard is never rendered for them.
    if authorize(g.user["role"], "manage_users") and not authorize(g.user["role"], "read_notes"):
        return redirect(url_for("admin.users_view"))

    role = g.user["role"]
    conn = get_db()
    # P23: the CLINIC's today (Europe/Rome), never the server's date (F1)
    now = clinic_time.now()
    today = now.date().isoformat()

    # withheld, not hidden: the capability decides whether each query runs at all
    may_book = authorize(role, "manage_appointments")
    agenda = _agenda_view(appointments.agenda(conn, today), now) if may_book else None
    pending = appointments.pending_requests(conn) if may_book else None
    reviews = (conn.execute("SELECT COUNT(*) FROM note_reviews WHERE status IN"
                            " ('pending', 'extraction_failed', 'confirming')").fetchone()[0]
               if authorize(role, "review_upload") else None)
    stock_low = len(inventory.open_alerts(conn)) if authorize(role, "use_inventory") else None
    callbacks = (conn.execute("SELECT COUNT(*) FROM handoff_requests WHERE status IN ('open', 'claimed')").fetchone()[0]
                 if authorize(role, "handle_handoff") else None)

    # the review-and-alerts list: work other than requests, each linked to where it is done
    alerts = []
    if reviews:
        alerts.append((f"{reviews} note{'s' if reviews != 1 else ''} awaiting a dentist", url_for("review.queue")))
    if stock_low:
        alerts.append((f"{stock_low} item{'s' if stock_low != 1 else ''} low on stock", url_for("stock.index")))
    if callbacks:
        alerts.append((f"{callbacks} call-back{'s' if callbacks != 1 else ''} open", url_for("handoff.index")))
    attention = []
    if pending:
        attention.append((f"{len(pending)} request{'s' if len(pending) != 1 else ''} to confirm", url_for("appointments.index") + "#requests"))
    attention += alerts

    kpis = []
    if may_book:
        kpis.append(("Appointments today", len(agenda), "booked on the schedule", url_for("appointments.index", day=today), "bi-calendar3"))
        kpis.append(("Requests to confirm", len(pending), "not bookings yet", url_for("appointments.index") + "#requests", "bi-inbox"))
    if reviews is not None:
        kpis.append(("Notes to review", reviews, "a dentist confirms them", url_for("review.queue"), "bi-clipboard-check"))
    if stock_low is not None:
        kpis.append(("Low stock", stock_low, "open stock alerts", url_for("stock.index"), "bi-box-seam"))

    # a quiet day says so, and offers the next day that has bookings (a real link, not a filler row)
    next_day = None
    if may_book and not agenda:
        lo, _ = clinic_time.day_bounds_utc((now.date() + timedelta(days=1)).isoformat())
        row = conn.execute("SELECT starts_at FROM appointments WHERE status = ? AND starts_at >= ?"
                           " ORDER BY starts_at LIMIT 1", (appointments.BOOKED, lo)).fetchone()
        if row:
            d = clinic_time.local_of(row["starts_at"]).date()
            next_day = {"iso": d.isoformat(), "label": f"{d:%a} {d.day} {d:%b}"}

    show_intake = authorize(role, "upload_file")
    intake = _intake_counts(conn, g.user["username"]) if show_intake else None
    return render_template(
        "dashboard.html",
        today_label=f"{now:%A} {now.day} {now:%B %Y}",
        may_book=may_book, agenda=agenda, requests=_request_view(pending) if pending else [],
        request_total=len(pending) if pending is not None else None,
        attention=attention, alerts=alerts, kpis=kpis, next_day=next_day,
        activity=_activity(conn, g.user["username"]) if authorize(role, "read_notes") else [],
        show_intake=show_intake, intake=intake,
    )


_TOOL_WORDS = {"update_field": "Record detail changed", "update_visit_field": "Visit detail changed",
               "add_invoice": "Invoice line added"}
_FIELD_WORDS = {"patients.phone": "phone", "patients.patient_name": "name",
                "visits.next_appointment": "recall", "visits.visit_date": "visit date"}
_STATE_WORDS = {"sorted": "Note filed", "needs_review": "File needs review", "awaiting_review": "Note waiting for a dentist",
                "not_searchable": "Filed, not searchable yet", "queued": "Upload queued", "external": "File routed by the watcher",
                "rejected": "Upload refused"}


def _who(conn, key):
    """a patient's name from a patient id or codice fiscale - never the identifier itself."""
    import patient_id
    pid = patient_id.resolve(conn, key) if key else None
    row = conn.execute("SELECT patient_name FROM patients WHERE patient_id = ?", (pid,)).fetchone() if pid else None
    return row[0] if row else None


def _activity(conn, username, limit=8):
    """P23 (F4): the user's recent work in plain words - who and what, clinic time, no paths or codici fiscali."""
    items = []
    for i, entry in enumerate(_user_undo_history(username, limit=limit)):
        field = (entry.get("target") or "").split(":")[1] if ":" in (entry.get("target") or "") else ""
        who = _who(conn, entry.get("codice_fiscale"))
        what = _TOOL_WORDS.get(entry.get("tool"), "Record changed")
        detail = _FIELD_WORDS.get(field.rsplit(":", 1)[0], "")
        try:
            when = clinic_time.local_of(entry["ts"]).strftime("%-d %b, %H:%M")     # stored in UTC
        except (KeyError, ValueError):
            # an entry from before stamps were UTC has no known zone: its day, not a guessed time
            when = (entry.get("ts") or "")[:10]
        items.append({"when": when, "sort": entry.get("ts", ""), "text": f"{what}{' (' + detail + ')' if detail else ''}",
                      "who": who or "a patient no longer on record", "undo": i == 0})
    rows = conn.execute("SELECT ts, target, action, allowed, reason FROM audit_log WHERE username = ? AND action IN"
                        " ('queue_upload', 'upload_file', 'sync_note', 'note_staged') ORDER BY id DESC LIMIT ?",
                        (username, limit)).fetchall()
    for r in rows:
        parts = Path(r["target"] or "").parts
        who = _who(conn, parts[1]) if len(parts) > 2 and parts[0] == "sorted" else None
        items.append({"when": clinic_time.local_of(r["ts"]).strftime("%-d %b, %H:%M") if r["ts"] else "",
                      "sort": r["ts"] or "", "text": _STATE_WORDS[_intake_state(r)],
                      "who": who or "no patient matched yet", "undo": False})
    items.sort(key=lambda x: x["sort"], reverse=True)
    return items[:limit]
