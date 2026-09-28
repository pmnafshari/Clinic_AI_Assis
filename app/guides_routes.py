"""Ask clinic guides and the guides library (P24). Staff only; admin and patients have no route here.

Every decision is checked again in clinic_guides; these routes add the staff audit trail (never the question
text) and read the staff users table only to check that a named approver is a real, active, non-admin account.
"""
from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for

import clinic_guides as cg
from auth import authorize, log_audit

from .db import get_db

guides_bp = Blueprint("guides", __name__)

EXAMPLES = ["What does the B-PROG button do?", "Cosa significa l'errore E07?",
            "The dentist has already ordered an OPG. What does reception do next?"]
REASON_TITLES = {"ask_device": "Which device?", "wrong_model": "Different model", "old_edition": "Not the current edition",
                 "conflict": "Approved documents disagree", "unreadable": "The page cannot be read reliably",
                 "restricted": "Not for your role", "servicing": "Service work", "clinical": "A decision for the dentist",
                 "patient_data": "No patient information here", "not_for_role": "Not for your role",
                 "not_found": "No approved document answers this"}


def gconn():
    if "guides" not in g:
        g.guides = cg.connect()
    return g.guides


@guides_bp.teardown_app_request
def _close(_exc):
    conn = g.pop("guides", None)
    if conn is not None:
        conn.close()


def _who():
    return g.user["username"], g.user["role"]


def _refuse(action, target="guides"):
    log_audit(get_db(), g.user["username"], g.user["role"], action, target, allowed=0)
    flash("Clinic guides are for the dentist and reception.", "danger")
    return redirect(url_for("dashboard.index"))


def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store"
    return resp


@guides_bp.route("/guides/ask", methods=["GET", "POST"])
def ask():
    if not authorize(g.user["role"], cg.ASK):
        return _refuse("guide_ask")
    conn = gconn()
    result, question, device_id = None, "", None
    if request.method == "POST":
        question = (request.form.get("question") or "").strip()[:500]
        raw = request.form.get("device_id") or ""
        device_id = int(raw) if raw.isdigit() else None
        if question:
            model = cg.model_answer if request.form.get("explain") == "1" else None
            result = cg.ask(conn, question, g.user["role"], device_id=device_id, actor=g.user["username"],
                            model=model)
            cited = ",".join(f"{c['source_id']}:{c['page']}" for c in result["citations"])
            log_audit(get_db(), g.user["username"], g.user["role"], "guide_ask", cited or "none", allowed=1,
                      reason=result["reason"] or "answered")
    titles = {r["id"]: r for r in conn.execute("SELECT s.id, s.title, s.edition, s.version, s.language, d.make,"
                                               " d.model, d.room FROM sources s LEFT JOIN devices d ON d.id ="
                                               " s.device_id")}
    resp = Response(render_template("guides_ask.html", devices=cg.devices(conn), result=result, question=question,
                                    device_id=device_id, examples=EXAMPLES, titles=titles, label=cg.device_label,
                                    reason_titles=REASON_TITLES))
    return _no_store(resp)


@guides_bp.route("/guides")
def library():
    if not authorize(g.user["role"], cg.ASK):
        return _refuse("guide_library")
    conn = gconn()
    rows = cg.library(conn, *_who())
    approvers = conn.execute("SELECT * FROM approvers WHERE active = 1 ORDER BY username").fetchall() \
        if authorize(g.user["role"], cg.APPROVE) else []
    staff = get_db().execute("SELECT username FROM users WHERE active = 1 AND role != 'admin' ORDER BY username"
                             ).fetchall() if authorize(g.user["role"], cg.APPROVE) else []
    return _no_store(Response(render_template(
        "guides_library.html", rows=rows, devices=cg.devices(conn), label=cg.device_label,
        manage=authorize(g.user["role"], cg.MANAGE), approve=authorize(g.user["role"], cg.APPROVE),
        approvers=approvers, staff=staff)))


@guides_bp.route("/guides/devices", methods=["POST"])
def add_device():
    if not authorize(g.user["role"], cg.MANAGE):
        return _refuse("guide_device")
    try:
        did = cg.add_device(gconn(), request.form.get("make"), request.form.get("model"), request.form.get("room"),
                            *_who(), type_words=request.form.get("type_words", ""))
    except cg.GuideError as e:
        flash(str(e), "danger")
        return redirect(url_for("guides.library"))
    log_audit(get_db(), *_who(), "guide_device", f"device:{did}", allowed=1)
    flash("Device registered.", "success")
    return redirect(url_for("guides.library"))


@guides_bp.route("/guides/upload", methods=["POST"])
def upload():
    if not authorize(g.user["role"], cg.MANAGE):
        return _refuse("guide_upload")
    blob = request.files.get("file")
    if blob is None or not blob.filename:
        flash("Choose a PDF to upload.", "danger")
        return redirect(url_for("guides.library"))
    f = request.form
    raw = f.get("device_id") or ""
    try:
        guide_id = cg.ingest(gconn(), blob.read(10 * 1024 * 1024 + 1), blob.filename, *_who(), title=f.get("title", ""),
                        kind=f.get("kind", ""), device_id=int(raw) if raw.isdigit() else None,
                        edition=f.get("edition", ""), language=f.get("language", ""), version=f.get("version", ""),
                        owner=f.get("owner", ""), audience=f.get("audience", ""), effective=f.get("effective", ""))
    except cg.GuideError as e:
        flash(str(e), "danger")
        return redirect(url_for("guides.library"))
    log_audit(get_db(), *_who(), "guide_upload", f"guide:{guide_id}", allowed=1)
    status = cg.source(gconn(), guide_id)["status"]
    flash({"pending_review": "Read. Review every page before approving it; until then nobody can ask about it.",
           "quarantined": "Not accepted: the file was kept but not read.",
           "extraction_failed": "The file was kept, but it could not be read."}.get(status, "Saved."),
          "success" if status == "pending_review" else "danger")
    return redirect(url_for("guides.source_view", guide_id=guide_id))


@guides_bp.route("/guides/sources/<int:guide_id>")
def source_view(guide_id):
    if not authorize(g.user["role"], cg.ASK):
        return _refuse("guide_read", f"guide:{guide_id}")
    conn = gconn()
    s = cg.source(conn, guide_id)
    if s is None:
        abort(404)
    reviewer = cg.may_approve(conn, s, *_who())
    visible = [p for p in cg.pages(conn, guide_id) if cg.page_for(conn, guide_id, p["page"], *_who()) is not None]
    if not visible and not (reviewer and s["status"] in ("quarantined", "extraction_failed", "rejected", "withdrawn",
                                                             "superseded")):
        abort(404)
    if s["status"] != "approved" and not reviewer:
        abort(404)
    log_audit(get_db(), *_who(), "guide_read", f"guide:{guide_id}", allowed=1)
    device = conn.execute("SELECT * FROM devices WHERE id = ?", (s["device_id"],)).fetchone() if s["device_id"] else None
    hidden = len(cg.pages(conn, guide_id)) - len(visible)
    import json as _json
    return _no_store(Response(render_template(
        "guides_source.html", s=s, pages=visible, hidden=hidden, device=device, label=cg.device_label,
        reviewer=reviewer, approve=authorize(g.user["role"], cg.APPROVE), history=cg.history(conn, guide_id),
        focus=request.args.get("page", type=int), flags={p["page"]: _json.loads(p["flags"]) for p in visible},
        others=conn.execute("SELECT id, title, edition, version FROM sources WHERE status = 'approved' AND id != ?"
                            " AND kind = ? AND COALESCE(device_id, 0) = COALESCE(?, 0)",
                            (guide_id, s["kind"], s["device_id"])).fetchall(),
        readable_at=cg.READABLE_AT)))


@guides_bp.route("/guides/sources/<int:guide_id>/pages/<int:page>/image")
def page_image(guide_id, page):
    if not authorize(g.user["role"], cg.ASK):
        return _refuse("guide_read", f"guide:{guide_id}")
    p = cg.page_for(gconn(), guide_id, page, *_who())
    if p is None or not p["image_path"]:
        abort(404)
    path = (cg.STORE / p["image_path"]).resolve()
    if cg.STORE.resolve() not in path.parents or not path.is_file():
        abort(404)
    resp = Response(path.read_bytes(), mimetype="image/png")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
    return _no_store(resp)


def _decide(guide_id, fn, done, action, **kwargs):
    try:
        fn(gconn(), guide_id, *kwargs.pop("args", ()), actor=g.user["username"], role=g.user["role"], **kwargs)
    except LookupError:
        abort(404)
    except PermissionError:
        log_audit(get_db(), *_who(), action, f"guide:{guide_id}", allowed=0)
        flash("You may not make this decision.", "danger")
        return redirect(url_for("guides.library"))
    except cg.GuideError as e:
        flash(str(e), "danger")
        return redirect(url_for("guides.source_view", guide_id=guide_id))
    log_audit(get_db(), *_who(), action, f"guide:{guide_id}", allowed=1)
    flash(done, "success")
    return redirect(url_for("guides.source_view", guide_id=guide_id))


@guides_bp.route("/guides/sources/<int:guide_id>/approve", methods=["POST"])
def approve(guide_id):
    if not authorize(g.user["role"], cg.ASK):
        return _refuse("guide_approve", f"guide:{guide_id}")
    return _decide(guide_id, cg.approve, "Approved. Staff can ask about it from now on.", "guide_approve")


@guides_bp.route("/guides/sources/<int:guide_id>/reject", methods=["POST"])
def reject(guide_id):
    if not authorize(g.user["role"], cg.ASK):
        return _refuse("guide_reject", f"guide:{guide_id}")
    return _decide(guide_id, cg.reject, "Rejected. It will never be searched.", "guide_reject",
                   args=(request.form.get("reason", ""),))


@guides_bp.route("/guides/sources/<int:guide_id>/withdraw", methods=["POST"])
def withdraw(guide_id):
    if not authorize(g.user["role"], cg.ASK):
        return _refuse("guide_withdraw", f"guide:{guide_id}")
    return _decide(guide_id, cg.withdraw, "Withdrawn. It is out of every answer from now on.", "guide_withdraw",
                   args=(request.form.get("reason", ""),))


@guides_bp.route("/guides/sources/<int:guide_id>/restrict", methods=["POST"])
def restrict(guide_id):
    if not authorize(g.user["role"], cg.APPROVE):
        return _refuse("guide_restrict", f"guide:{guide_id}")
    pages = [int(n) for n in request.form.getlist("restricted") if n.isdigit()]
    return _decide(guide_id, cg.restrict_pages, "Saved. Restricted pages are for the dentist only.", "guide_restrict",
                   args=(pages,))


@guides_bp.route("/guides/sources/<int:guide_id>/supersede", methods=["POST"])
def supersede(guide_id):
    if not authorize(g.user["role"], cg.APPROVE):
        return _refuse("guide_supersede", f"guide:{guide_id}")
    old = request.form.get("old_id", "")
    if not old.isdigit():
        flash("Choose the edition this one replaces.", "danger")
        return redirect(url_for("guides.source_view", guide_id=guide_id))
    try:
        cg.supersede(gconn(), int(old), guide_id, *_who())
    except (cg.GuideError, LookupError) as e:
        flash(str(e), "danger")
        return redirect(url_for("guides.source_view", guide_id=guide_id))
    log_audit(get_db(), *_who(), "guide_supersede", f"guide:{old}", allowed=1, reason=f"guide:{guide_id}")
    flash("The earlier edition is replaced and no longer answers.", "success")
    return redirect(url_for("guides.source_view", guide_id=guide_id))


@guides_bp.route("/guides/approvers", methods=["POST"])
def approvers():
    if not authorize(g.user["role"], cg.APPROVE):
        return _refuse("guide_approver")
    username = (request.form.get("username") or "").strip()
    user = get_db().execute("SELECT username, role FROM users WHERE username = ? AND active = 1",
                            (username,)).fetchone()
    if user is None or user["role"] == "admin" or not authorize(user["role"], cg.ASK):
        flash("Name an active dentist or reception account; admin accounts cannot approve documents.", "danger")
        return redirect(url_for("guides.library"))
    if request.form.get("action") == "revoke":
        cg.revoke_approver(gconn(), username, *_who())
    else:
        cg.designate_approver(gconn(), username, *_who())
    log_audit(get_db(), *_who(), "guide_approver", username, allowed=1, reason=request.form.get("action") or "name")
    flash("Saved.", "success")
    return redirect(url_for("guides.library"))
