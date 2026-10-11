"""Jarvis (J00) in the clinic app: device registration (admin), session delegation (staff), and the device API
(whoami since J00, clinic-guide questions since J02, a note added by voice since J03 - drafted, then saved only when
the person at the device's screen picks the patient)."""
from pathlib import Path

from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, url_for

import jarvis_link as jl
import web_session
from auth import authorize, log_audit

from .db import get_chroma, get_db
from .guides_routes import REASON_TITLES, gconn

jarvis_bp = Blueprint("jarvis", __name__)
SORTED_ROOT = Path("sorted")
NOTE_STATUS = {"auth_required": 403, "role": 403, "empty": 400, "no_match": 404, "too_many": 409, "patient": 409,
               "gone": 410}


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
    info["approved_guides"] = jl.approved_guides(gconn())       # the Jarvis page says when there is nothing to ask
    resp = jsonify(info)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@jarvis_bp.route("/api/jarvis/guides/ask", methods=["POST"])
def api_guides_ask():
    """A device's spoken clinic-guide question (J02). Device credential only, no staff cookie, so no CSRF token: the
    request carries nothing a browser would send by itself. Audited without the question."""
    conn = get_db()
    header = request.headers.get("Authorization", "")
    try:
        device = jl.authenticate(conn, header[7:] if header.startswith("Bearer ") else "")
    except jl.LinkError:
        log_audit(conn, "jarvis-device", "device", "jarvis_api", "guides_ask", allowed=0)
        return jsonify({"error": "not a registered Jarvis device"}), 401
    actor = f"jarvis-device:{device['id']}"
    body = request.get_json(silent=True)
    question = body.get("question") if isinstance(body, dict) else None
    if not isinstance(question, str) or not question.strip():
        log_audit(conn, actor, "device", "jarvis_api", "guides_ask", allowed=0)
        return jsonify({"error": "send {\"question\": \"...\"}"}), 400
    result = jl.ask_guides(gconn(), device, question)
    result["title"] = REASON_TITLES.get(result.get("reason"))     # the same title the staff page gives a refusal
    log_audit(conn, actor, "device", "jarvis_api", "guides_ask", allowed=1)
    resp = jsonify(result)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@jarvis_bp.route("/api/jarvis/guides/vocabulary")
def api_guides_vocabulary():
    """The library's own terms, as a hint for the device's speech to text (J-D10). Device credential only; audited
    without the terms."""
    conn = get_db()
    header = request.headers.get("Authorization", "")
    try:
        device = jl.authenticate(conn, header[7:] if header.startswith("Bearer ") else "")
    except jl.LinkError:
        log_audit(conn, "jarvis-device", "device", "jarvis_api", "guides_vocabulary", allowed=0)
        return jsonify({"error": "not a registered Jarvis device"}), 401
    ranked = jl.ranked_terms(gconn(), conn)
    terms = ranked[:jl.MAX_TERMS]
    log_audit(conn, f"jarvis-device:{device['id']}", "device", "jarvis_api", "guides_vocabulary", allowed=1)
    resp = jsonify({"terms": terms, "dropped": len(ranked) - len(terms)})
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _device(conn, action):
    header = request.headers.get("Authorization", "")
    try:
        return jl.authenticate(conn, header[7:] if header.startswith("Bearer ") else "")
    except jl.LinkError:
        log_audit(conn, "jarvis-device", "device", "jarvis_api", action, allowed=0)
        return None


def _json(body, status=200):
    resp = jsonify(body)
    resp.status_code = status
    resp.headers["Cache-Control"] = "no-store"
    return resp


@jarvis_bp.route("/api/jarvis/notes/draft", methods=["POST"])
def api_note_draft():
    """A note heard by a device, for a spoken patient name (J03). Device credential only; the delegated person's live
    session and role decide; the matching patients come back for the screen - nothing is written."""
    conn = get_db()
    device = _device(conn, "note_draft")
    if device is None:
        return _json({"error": "not a registered Jarvis device"}, 401)
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("patient"), str) or not isinstance(body.get("text"), str):
        return _json({"error": "send {\"patient\": \"...\", \"text\": \"...\"}"}, 400)
    try:
        return _json(jl.draft_note(conn, device, body["patient"], body["text"]))
    except jl.LinkError as e:
        return _json({"error": e.code, "message": str(e)}, NOTE_STATUS.get(e.code, 409))


@jarvis_bp.route("/api/jarvis/notes/commit", methods=["POST"])
def api_note_commit():
    """Save a drafted note on the patient picked on the device's screen, through the clinic's own save."""
    conn = get_db()
    device = _device(conn, "note_commit")
    if device is None:
        return _json({"error": "not a registered Jarvis device"}, 401)
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("draft"), str) or not isinstance(body.get("cf"), str):
        return _json({"error": "send {\"draft\": \"...\", \"cf\": \"...\"}"}, 400)
    from storage import save_new_note

    def save(note, role, username):
        return save_new_note(note, conn, get_chroma(), role, username, sorted_root=SORTED_ROOT)
    try:
        return _json(jl.commit_note(conn, device, body["draft"], body["cf"], save))
    except jl.LinkError as e:
        return _json({"error": e.code, "message": str(e)}, NOTE_STATUS.get(e.code, 409))
