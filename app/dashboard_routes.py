import json
from pathlib import Path

from flask import Blueprint, g, redirect, render_template, url_for

import agent
from auth import authorize

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
    # phrased as a capability pair so it survives a role being renamed.
    if authorize(g.user["role"], "manage_users") and not authorize(g.user["role"], "read_notes"):
        return redirect(url_for("admin.users_view"))

    conn = get_db()
    history = _user_undo_history(g.user["username"])

    # withheld, not hidden (D-09/D-10, RBAC-03/04). the gate decides whether
    # the query RUNS, so a role that may not see a figure gets a response
    # with no trace of it - not a hidden one. same shape as
    # patients_routes.detail_view's clinical card.
    show_intake = authorize(g.user["role"], "upload_file")
    intake_counts = _intake_counts(conn, g.user["username"]) if show_intake else None

    # visit and patient totals are a view of the clinical record in
    # aggregate, so they sit behind read_clinical - the dentist-only
    # capability, never read_notes, which assistant also holds (RBAC-03)
    show_clinical = authorize(g.user["role"], "read_clinical")
    if show_clinical:
        visit_months, visit_counts = _visits_by_month(conn)
        patient_total = conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0]
    else:
        visit_months = None
        visit_counts = None
        patient_total = None

    # whether a chart has anything to draw is decided here too, for the same
    # reason the series is (D-01). chart.js draws nothing for an all-zero
    # doughnut and an empty bar chart, and reports neither - so an empty card
    # looks exactly like a broken one. the template needs a verdict, not a sum.
    #
    # this is NOT the same question as show_intake/show_clinical. those decide
    # whether the query runs at all; a role that may not see a figure gets no
    # canvas and no empty state either. permitted-with-no-data and
    # not-permitted are different renders and must not collapse into one flag.
    intake_has_data = show_intake and any(intake_counts.values())
    visits_have_data = show_clinical and bool(visit_months)

    # chart series are shaped here, not in jinja: the template renders what
    # it is given and computes nothing (D-01, as phase 23 did for the badge)
    return render_template(
        "dashboard.html",
        user=g.user,
        history=history,
        show_intake=show_intake,
        intake_counts=intake_counts,
        intake_chart_labels=[label for _, label in INTAKE_LABELS] if show_intake else None,
        intake_chart_values=(
            [intake_counts[state] for state, _ in INTAKE_LABELS] if show_intake else None
        ),
        intake_has_data=intake_has_data,
        show_clinical=show_clinical,
        visit_months=visit_months,
        visit_counts=visit_counts,
        visits_have_data=visits_have_data,
        patient_total=patient_total,
    )
