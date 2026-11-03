from flask import Blueprint, flash, g, redirect, render_template, request, url_for

import handoff
import providers
from auth import authorize, log_audit

from .db import get_db

handoff_bp = Blueprint("handoff", __name__)


def _refuse(action, target=None):
    log_audit(get_db(), g.user["username"], g.user["role"], action, target, allowed=0)
    flash("You don't have permission to work the call-back queue.", "danger")
    return redirect(url_for("dashboard.index"))


@handoff_bp.route("/handoffs")
def index():
    if not authorize(g.user["role"], "handle_handoff"):
        return _refuse("view_handoffs")
    conn = get_db()
    status = request.args.get("status")
    rows = handoff.queue(conn, status if status in (handoff.OPEN, handoff.CLAIMED,
                                                    handoff.RESOLVED) else None)
    open_now, nxt = handoff.next_opening(conn)
    # P29 R-019: while the phone line is off every call-back came from the portal
    phone_line, _ = providers.allowed(conn, "telephony")
    return render_template("handoffs.html", rows=rows, counts=handoff.counts(conn),
                           status=status, reasons=handoff.REASONS,
                           open_now=open_now, next_opening=nxt, phone_line=phone_line,
                           me=g.user["username"])


def _whose(row):
    # P29 R-019: a message names the patient and the topic, not just "that one"
    if row is None:
        return "this call-back"
    return f"the call-back for {row['patient_name'] or 'an erased patient'} ({row['topic']})"


@handoff_bp.route("/handoffs/<int:handoff_id>/claim", methods=["POST"])
def claim(handoff_id):
    if not authorize(g.user["role"], "handle_handoff"):
        return _refuse("claim_handoff", f"handoff:{handoff_id}")
    conn = get_db()
    try:
        took = handoff.claim(conn, handoff_id, g.user["username"], g.user["role"])
    except PermissionError:
        return _refuse("claim_handoff", f"handoff:{handoff_id}")
    row = handoff.get(conn, handoff_id)
    if took:
        flash(f"You have taken {_whose(row)}. Call them back, then mark it done.", "success")
    elif row is None:
        flash("That call-back is not in the queue.", "danger")
    elif row["status"] == handoff.RESOLVED:
        flash(f"{_whose(row).capitalize()} was already marked done by {row['resolved_by']}.", "warning")
    else:
        flash(f"{row['claimed_by']} took it first - {_whose(row)} is theirs now.", "warning")
    return redirect(url_for("handoff.index", status=request.form.get("status") or None))


@handoff_bp.route("/handoffs/<int:handoff_id>/resolve", methods=["POST"])
def resolve(handoff_id):
    if not authorize(g.user["role"], "handle_handoff"):
        return _refuse("resolve_handoff", f"handoff:{handoff_id}")
    conn = get_db()
    row = handoff.get(conn, handoff_id)
    try:
        done = handoff.resolve(conn, handoff_id, g.user["username"], g.user["role"])
    except PermissionError:
        flash(f"{row['claimed_by']} has taken {_whose(row)}; only they or a dentist can mark it done.", "danger")
        return redirect(url_for("handoff.index"))
    if done:
        flash(f"Marked done: {_whose(row)}.", "success")
    elif row is None:
        flash("That call-back is not in the queue.", "danger")
    else:
        flash(f"{_whose(row).capitalize()} was already marked done.", "warning")
    return redirect(url_for("handoff.index", status=request.form.get("status") or None))
