"""Jarvis (J00) in the clinic app: device registration (admin), session delegation (staff), and the device API."""
from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, url_for

import jarvis_link as jl
import web_session
from auth import authorize, log_audit

from .db import get_db

jarvis_bp = Blueprint("jarvis", __name__)


def _who():
    return g.user["username"], g.user["role"]


def _refuse(action, target="jarvis"):
    log_audit(get_db(), g.user["username"], g.user["role"], action, target, allowed=0)
    flash("You do not have access to that.", "danger")
    return redirect(url_for("dashboard.index"))


@jarvis_bp.route("/jarvis/devices", methods=["GET", "POST"])
def devices():
    if not authorize(g.user["role"], "manage_users"):
        return _refuse("jarvis_devices")
    shown = None
    if request.method == "POST":
        try:
            device_id, shown = jl.register_device(get_db(), request.form.get("name", ""), *_who())
        except jl.LinkError as e:
            flash(str(e), "danger")
    return render_template("jarvis_devices.html", devices=jl.devices(get_db()), shown=shown)


@jarvis_bp.route("/jarvis/devices/<int:device_id>/revoke", methods=["POST"])
def revoke_device(device_id):
    if not authorize(g.user["role"], "manage_users"):
        return _refuse("jarvis_device_revoke", f"jarvis_device:{device_id}")
    jl.revoke_device(get_db(), device_id, *_who())
    flash("Device revoked: it can no longer reach the clinic app.", "success")
    return redirect(url_for("jarvis.devices"))


@jarvis_bp.route("/jarvis/link")
def link():
    if not authorize(g.user["role"], "use_jarvis"):
        return _refuse("jarvis_link")
    conn = get_db()
    return render_template("jarvis_link.html", devices=[d for d in jl.devices(conn) if d["revoked_at"] is None],
                           mine=jl.delegations_for(conn, g.user["username"]), hours=jl.DELEGATION_HOURS)


@jarvis_bp.route("/jarvis/link/<int:device_id>", methods=["POST"])
def delegate(device_id):
    if not authorize(g.user["role"], "use_jarvis"):
        return _refuse("jarvis_delegate", f"jarvis_device:{device_id}")
    try:
        jl.delegate(get_db(), device_id, request.cookies.get(web_session.COOKIE_NAME), *_who())
        flash("Jarvis on this device now acts for your session. It ends when you sign out or go idle.", "success")
    except jl.LinkError as e:
        flash(str(e), "danger")
    return redirect(url_for("jarvis.link"))


@jarvis_bp.route("/jarvis/link/delegations/<int:delegation_id>/revoke", methods=["POST"])
def revoke_delegation(delegation_id):
    if not authorize(g.user["role"], "use_jarvis"):
        return _refuse("jarvis_delegation_revoke", f"jarvis_delegation:{delegation_id}")
    try:
        jl.revoke_delegation(get_db(), delegation_id, *_who())
        flash("Jarvis no longer acts for your session.", "success")
    except jl.LinkError as e:
        flash(str(e), "danger")
    return redirect(url_for("jarvis.link"))


@jarvis_bp.route("/api/jarvis/whoami")
def api_whoami():
    """The device's own view. Device credential only (no staff cookie); every call audited; a refusal says nothing."""
    conn = get_db()
    header = request.headers.get("Authorization", "")
    bearer = header[7:] if header.startswith("Bearer ") else ""
    try:
        info = jl.whoami(conn, bearer)
    except jl.LinkError:
        log_audit(conn, "jarvis-device", "device", "jarvis_api", "whoami", allowed=0)
        return jsonify({"error": "not a registered Jarvis device"}), 401
    log_audit(conn, f"jarvis-device:{info['device_id']}", "device", "jarvis_api", "whoami", allowed=1)
    resp = jsonify(info)
    resp.headers["Cache-Control"] = "no-store"
    return resp
