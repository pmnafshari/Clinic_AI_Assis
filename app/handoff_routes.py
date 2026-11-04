from flask import Blueprint, flash, g, redirect, render_template, request, url_for

import handoff
import providers
from auth import authorize, log_audit

from .db import get_db
from .i18n import _

handoff_bp = Blueprint("handoff", __name__)


def _refuse(action, target=None):
    log_audit(get_db(), g.user["username"], g.user["role"], action, target, allowed=0)
    flash(_("You don't have permission to work the call-back queue."), "danger")
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
    phone_line, _why = providers.allowed(conn, "telephony")
    return render_template("handoffs.html", rows=rows, counts=handoff.counts(conn),
                           status=status, reasons=handoff.REASONS,
                           open_now=open_now, next_opening=nxt, phone_line=phone_line,
                           me=g.user["username"])


def _whose(row):
    # P29 R-019: a message names the patient and the topic, not just "that one"
    if row is None:
        return _("this call-back")
    return _("the call-back for {name} ({topic})", name=row["patient_name"] or _("an erased patient"), topic=_(row["topic"]))


def _first_up(text):
    # a sentence that starts with a phrase: its first letter up, the rest - a patient's name too - as written
    return text[:1].upper() + text[1:]


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
        flash(_("You have taken {what}. Call them back, then mark it done.", what=_whose(row)), "success")
    elif row is None:
        flash(_("That call-back is not in the queue."), "danger")
    elif row["status"] == handoff.RESOLVED:
        flash(_first_up(_("{what} was already marked done by {who}.", what=_whose(row), who=row["resolved_by"])), "warning")
    else:
        flash(_("{who} took it first - {what} is theirs now.", who=row["claimed_by"], what=_whose(row)), "warning")
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
        flash(_("{who} has taken {what}; only they or a dentist can mark it done.", who=row["claimed_by"], what=_whose(row)), "danger")
        return redirect(url_for("handoff.index"))
    if done:
        flash(_("Marked done: {what}.", what=_whose(row)), "success")
    elif row is None:
        flash(_("That call-back is not in the queue."), "danger")
    else:
        flash(_first_up(_("{what} was already marked done.", what=_whose(row))), "warning")
    return redirect(url_for("handoff.index", status=request.form.get("status") or None))
