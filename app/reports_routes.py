import json

from flask import Blueprint, g, redirect, render_template, url_for

from auth import authorize
from dental_notes_schema import KNOWN_PROCEDURES

from .db import get_db

reports_bp = Blueprint("reports", __name__)

# how many bars the distribution shows before the tail is folded together. a
# chart with forty labels is a table that has been made harder to read.
TOP_N = 8


def _code_of(entry):
    # the SAME rule dental_notes_schema.unknown_procedures uses: the first
    # token is the code, the rest is tooth numbers. reusing it means this
    # report and the intake pipeline can never disagree about what a
    # procedure is called.
    return entry.strip().split(" ")[0].lower()


def _procedure_distribution(conn, top_n=TOP_N):
    # clinic-wide, counts only, never a procedure tied to a patient (D-07).
    rows = conn.execute(
        "SELECT procedures FROM visits"
        " WHERE procedures IS NOT NULL AND procedures != '' AND procedures != '[]'"
    ).fetchall()

    counts = {}
    for row in rows:
        try:
            entries = json.loads(row["procedures"])
        except (json.JSONDecodeError, TypeError):
            # a hand-edited or truncated row must not 500 the whole report
            continue
        for entry in entries:
            code = _code_of(entry)
            if code:
                counts[code] = counts.get(code, 0) + 1

    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    head = ordered[:top_n]
    tail = ordered[top_n:]

    labels = [code for code, _ in head]
    values = [n for _, n in head]
    # uncoded terms are shown, not tidied away: an unrecognised code is
    # flagged for review and still files (extract_note.py:48-50), so the
    # distribution is where that shows up honestly.
    uncoded = [code not in KNOWN_PROCEDURES for code in labels]

    if tail:
        labels.append(f"other ({len(tail)})")
        values.append(sum(n for _, n in tail))
        uncoded.append(False)

    return labels, values, uncoded


def _billed_by_month(conn, months=12):
    # invoices carry no status and there is no payments table, so this is
    # what was BILLED. it is never called revenue - nothing in this system
    # shows that any of it was collected.
    rows = conn.execute(
        "SELECT substr(v.visit_date, 1, 7) AS month, ROUND(SUM(i.amount), 2) AS billed"
        " FROM invoices i JOIN visits v ON v.id = i.visit_id"
        " WHERE v.visit_date IS NOT NULL AND v.visit_date != ''"
        " GROUP BY month ORDER BY month DESC LIMIT ?",
        (months,),
    ).fetchall()
    rows = list(reversed(rows))
    return [r["month"] for r in rows], [r["billed"] for r in rows]


@reports_bp.route("/reports")
def index():
    # aggregate views of the clinical record, so they sit behind
    # read_clinical - the dentist-only capability, never read_notes, which
    # assistant also holds (RBAC-03). withheld means the query never runs and
    # the page is never rendered, not that a card is hidden.
    if not authorize(g.user["role"], "read_clinical"):
        return redirect(url_for("dashboard.index"))

    conn = get_db()
    proc_labels, proc_values, proc_uncoded = _procedure_distribution(conn)
    billed_months, billed_values = _billed_by_month(conn)

    # the has-data verdict is a route decision, like the series it describes
    # (phase 31 D-01). chart.js draws nothing for an empty series and says
    # nothing about it, so an empty card looks exactly like a broken one.
    return render_template(
        "reports.html",
        proc_labels=proc_labels,
        proc_values=proc_values,
        proc_uncoded=proc_uncoded,
        proc_has_data=bool(proc_values),
        billed_months=billed_months,
        billed_values=billed_values,
        billed_has_data=bool(billed_values),
    )
