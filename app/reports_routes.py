import csv
import io
import json
from datetime import date

from flask import Blueprint, Response, g, redirect, render_template, request, url_for

import clinic_time
import ledger
from auth import authorize, log_audit
from dental_notes_schema import KNOWN_PROCEDURES

from .db import get_db
from .i18n import _, current

reports_bp = Blueprint("reports", __name__)

# how many bars the distribution shows before the tail is folded together. a
# chart with forty labels is a table that has been made harder to read.
TOP_N = 8


# P29 R-024: the period choices, months back (None for everything) and how the page says them
PERIODS = {"3": (3, "last 3 months"), "6": (6, "last 6 months"), "12": (12, "last 12 months"),
           "24": (24, "last 24 months"), "all": (None, "everything on record")}


def _code_of(entry):
    # the SAME rule dental_notes_schema.unknown_procedures uses: the first
    # token is the code, the rest is tooth numbers. reusing it means this
    # report and the intake pipeline can never disagree about what a
    # procedure is called.
    return entry.strip().split(" ")[0].lower()


def _procedure_distribution(conn, top_n=TOP_N, since=None):
    # clinic-wide, counts only, never a procedure tied to a patient (D-07).
    rows = conn.execute(
        "SELECT procedures FROM visits"
        " WHERE procedures IS NOT NULL AND procedures != '' AND procedures != '[]'"
        " AND (? IS NULL OR visit_date >= ?)", (since, since)
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
    values = [n for _label, n in head]
    # uncoded terms are shown, not tidied away: an unrecognised code is
    # flagged for review and still files (extract_note.py:48-50), so the
    # distribution is where that shows up honestly.
    uncoded = [code not in KNOWN_PROCEDURES for code in labels]

    if tail:
        labels.append(f"other ({len(tail)})")
        values.append(sum(n for _label, n in tail))
        uncoded.append(False)

    return labels, values, uncoded


def _billed_by_month(conn, months=12, since=None):
    # what was BILLED, from the lines' cents. it is never called revenue: the
    # ledger (P07) records payments staff typed in, and invoices from before
    # it are of unknown status - see _recorded_payments
    rows = conn.execute(
        "SELECT substr(v.visit_date, 1, 7) AS month, SUM(i.amount_cents) / 100.0 AS billed"
        " FROM invoices i JOIN visits v ON v.id = i.visit_id"
        " WHERE v.visit_date IS NOT NULL AND v.visit_date != '' AND (? IS NULL OR v.visit_date >= ?)"
        " GROUP BY month ORDER BY month DESC LIMIT ?",
        (since, since, months),
    ).fetchall()
    rows = list(reversed(rows))
    return [r["month"] for r in rows], [r["billed"] for r in rows]


def _recorded_payments(conn):
    # net of refunds and reversals; payments recorded by hand, not money seen
    # moving - there is no payment provider
    return conn.execute(
        "SELECT COALESCE(SUM(CASE kind WHEN 'payment' THEN amount_cents ELSE -amount_cents END), 0)"
        " FROM payments").fetchone()[0]


def _since(period):
    """the first day the period covers, counted in whole months back from the clinic's today; None for everything"""
    months = PERIODS[period][0]
    if not months:
        return None
    today = clinic_time.now().date()
    index = today.year * 12 + today.month - months
    return date(index // 12, index % 12 + 1, 1).isoformat()


def _my_work(conn, username, since):
    """P29 R-023/E-004: what this dentist did, read only from the rows that name them - their appointments and the
    notes they confirmed in review. visits record no author, so nothing else is counted as theirs. the username is
    always the signed-in one; no request names another dentist"""
    after = " AND a.starts_at >= ?" if since else ""
    appts = conn.execute(
        "SELECT a.starts_at, a.minutes, a.status, p.patient_name FROM appointments a JOIN patients p"
        " ON p.patient_id = a.patient_id WHERE a.dentist = ? AND a.status IN ('booked', 'cancelled')" + after +
        " ORDER BY a.starts_at DESC", (username, since) if since else (username,)).fetchall()
    after = " AND r.decided_at >= ?" if since else ""
    notes = conn.execute(
        "SELECT r.decided_at, p.patient_name, v.visit_date, v.procedures FROM note_reviews r JOIN patients p"
        " ON p.patient_id = r.patient_id LEFT JOIN visits v ON v.id = r.visit_id WHERE r.decided_by = ?"
        " AND r.status = 'confirmed'" + after + " ORDER BY r.decided_at DESC",
        (username, since) if since else (username,)).fetchall()
    appts = [{"when": clinic_time.local_date(a["starts_at"]) + " " + clinic_time.local_hhmm(a["starts_at"]),
              "minutes": a["minutes"], "status": a["status"], "patient": a["patient_name"]} for a in appts]
    notes = [{"decided": n["decided_at"][:10], "patient": n["patient_name"], "visit": n["visit_date"] or "",
              "procedures": ", ".join(json.loads(n["procedures"] or "[]"))} for n in notes]
    return appts, notes


def _cell(value):
    # a cell a spreadsheet would run as a formula is written as text (CSV injection)
    value = "" if value is None else str(value)
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value


def csv_download(rows, filename):
    out = io.StringIO()
    writer = csv.writer(out)
    for row in rows:
        writer.writerow([_cell(v) for v in row])
    resp = Response(out.getvalue(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    resp.headers["Cache-Control"] = "no-store"
    return resp


@reports_bp.route("/reports/mine.csv")
def mine_csv():
    """P29 E-004: the signed-in dentist's own report as a file - their rows only, decided here on the server"""
    user, role = g.user["username"], g.user["role"]
    if not authorize(role, "read_clinical"):
        log_audit(get_db(), user, role, "report_export", None, allowed=0)
        return redirect(url_for("dashboard.index"))
    conn = get_db()
    period = request.args.get("period") if request.args.get("period") in PERIODS else "12"
    appts, notes = _my_work(conn, user, _since(period))
    rows = [[_("Report of"), user, _("Period"), _(PERIODS[period][1])], [],
            [_("Appointments"), _("When"), _("Minutes"), _("Status"), _("Patient")]]
    rows += [["", a["when"], a["minutes"], _(a["status"]), a["patient"]] for a in appts]
    rows += [[], [_("Notes you confirmed"), _("Confirmed"), _("Visit date"), _("Patient"), _("Procedures, as written")]]
    rows += [["", n["decided"], n["visit"], n["patient"], n["procedures"]] for n in notes]
    log_audit(conn, user, role, "report_export", f"{user}:{period}:{len(appts)}+{len(notes)}", allowed=1)
    return csv_download(rows, f"report-{user}-{clinic_time.now().date().isoformat()}.csv")


@reports_bp.route("/reports")
def index():
    # aggregate views of the clinical record, so they sit behind
    # read_clinical - the dentist-only capability, never read_notes, which
    # assistant also holds (RBAC-03). withheld means the query never runs and
    # the page is never rendered, not that a card is hidden.
    if not authorize(g.user["role"], "read_clinical"):
        return redirect(url_for("dashboard.index"))

    conn = get_db()
    # P29 R-024: one period for every chart, counted in whole months back from the clinic's today
    period = request.args.get("period") if request.args.get("period") in PERIODS else "12"
    months = PERIODS[period][0]
    since = _since(period)
    my_appts, my_notes = _my_work(conn, g.user["username"], since)
    proc_labels, proc_values, proc_uncoded = _procedure_distribution(conn, since=since)
    billed_months, billed_values = _billed_by_month(conn, months or 600, since=since)
    # P23: visits per month moved here from Home (read_clinical, as it was there)
    from .dashboard_routes import _visits_by_month
    visit_months, visit_counts = _visits_by_month(conn, months or 600, since=since)

    # the has-data verdict is a route decision, like the series it describes
    # (phase 31 D-01). chart.js draws nothing for an empty series and says
    # nothing about it, so an empty card looks exactly like a broken one.
    return render_template(
        "reports.html",
        periods=[(k, v[1]) for k, v in PERIODS.items()], period=period, period_label=PERIODS[period][1], since=since,
        proc_labels=proc_labels,
        proc_values=proc_values,
        proc_uncoded=proc_uncoded,
        proc_has_data=bool(proc_values),
        billed_months=billed_months,
        recorded=ledger.fmt(_recorded_payments(conn), current()), recorded_cents=_recorded_payments(conn), fmt=ledger.fmt,
        my_appts=my_appts, my_notes=my_notes,
        billed_values=billed_values,
        billed_has_data=bool(billed_values),
        visit_months=visit_months,
        visit_counts=visit_counts,
        visits_have_data=bool(visit_months),
    )
