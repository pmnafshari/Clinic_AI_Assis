from flask import Blueprint, flash, g, redirect, render_template, request, url_for

import handoff
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
    return render_template("handoffs.html", rows=rows, counts=handoff.counts(conn),
                           status=status, reasons=handoff.REASONS,
                           open_now=open_now, next_opening=nxt,
                           me=g.user["username"])


@handoff_bp.route("/handoffs/<int:handoff_id>/claim", methods=["POST"])
def claim(handoff_id):
    if not authorize(g.user["role"], "handle_handoff"):
        return _refuse("claim_handoff", f"handoff:{handoff_id}")
    try:
        took = handoff.claim(get_db(), handoff_id, g.user["username"], g.user["role"])
    except PermissionError:
        return _refuse("claim_handoff", f"handoff:{handoff_id}")
    flash("Yours." if took else "Someone else got there first.",
          "success" if took else "danger")
    return redirect(url_for("handoff.index", status=request.form.get("status") or None))


@handoff_bp.route("/handoffs/<int:handoff_id>/resolve", methods=["POST"])
def resolve(handoff_id):
    if not authorize(g.user["role"], "handle_handoff"):
        return _refuse("resolve_handoff", f"handoff:{handoff_id}")
    try:
        done = handoff.resolve(get_db(), handoff_id, g.user["username"], g.user["role"])
    except PermissionError:
        flash("That one is held by a colleague. A dentist can close it.", "danger")
        return redirect(url_for("handoff.index"))
    flash("Closed." if done else "That one was already closed.",
          "success" if done else "danger")
    return redirect(url_for("handoff.index", status=request.form.get("status") or None))
