from flask import Blueprint, flash, g, redirect, render_template, request, url_for

import delivery
import providers
from auth import authorize, log_audit

from .db import get_db

providers_bp = Blueprint("providers", __name__)


def _refuse(action, target=None):
    log_audit(get_db(), g.user["username"], g.user["role"], action, target, allowed=0)
    flash("You don't have permission to see the provider switches.", "danger")
    return redirect(url_for("dashboard.index"))


@providers_bp.route("/providers")
def index():
    if not authorize(g.user["role"], "view_providers"):
        return _refuse("view_providers")
    conn = get_db()
    return render_template("providers.html", rows=providers.status(conn),
                           events=providers.recent_events(conn, 25),
                           receipts=delivery.counts(conn),
                           unresolved=delivery.unresolved(conn),
                           can_rearm=authorize(g.user["role"], "manage_providers"))


@providers_bp.route("/providers/<kind>/kill", methods=["POST"])
def kill(kind):
    if not authorize(g.user["role"], "view_providers"):
        return _refuse("provider_kill_on", kind)
    try:
        providers.set_kill(get_db(), kind, True, g.user["username"], g.user["role"])
        flash("Stopped. Nothing will go out until a dentist turns it back on.", "success")
    except (ValueError, PermissionError):
        return _refuse("provider_kill_on", kind)
    return redirect(url_for("providers.index"))


@providers_bp.route("/providers/<kind>/rearm", methods=["POST"])
def rearm(kind):
    if not authorize(g.user["role"], "manage_providers"):
        return _refuse("provider_kill_off", kind)
    try:
        providers.set_kill(get_db(), kind, False, g.user["username"], g.user["role"])
        flash("Switch cleared. The other brakes still apply.", "success")
    except (ValueError, PermissionError):
        return _refuse("provider_kill_off", kind)
    return redirect(url_for("providers.index"))
