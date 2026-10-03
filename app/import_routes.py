"""Legacy import review (P25). Progress for reception; evidence and decisions for a dentist only."""
from pathlib import Path

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for

import documents as docs
import drive_source as ds
import legacy_import as li
import patient_files as pf
from auth import authorize, log_audit

from .db import get_db

imports_bp = Blueprint("imports", __name__)

STATE_LABELS = {"proposed": "Proposed", "unmatched": "Unmatched", "conflict": "Conflict",
                "held": "Held for investigation", "confirmed": "Confirmed", "rejected": "Rejected",
                "refused": "Refused - not read", "staged": "Waiting to be read", "discovered": "Found"}
STRENGTH = {"strong": "strong: a checked code in a written field, confirmed by a second clue",
            "check": "needs checking: the evidence is weaker or only in a scan or file name"}


def _who():
    return g.user["username"], g.user["role"]


def _refuse(action, target="import"):
    log_audit(get_db(), g.user["username"], g.user["role"], action, target, allowed=0)
    flash("Only a dentist can review imported files.", "danger")
    return redirect(url_for("dashboard.index"))


def _people(conn, pids):
    pids = [p for p in pids if p]
    if not pids:
        return {}
    marks = ",".join("?" * len(pids))
    return {r["patient_id"]: r for r in conn.execute(
        f"SELECT patient_id, codice_fiscale, patient_name FROM patients WHERE patient_id IN ({marks})", pids)}


def _page(check=None, checked=None):
    conn = get_db()
    progress = li.progress(conn, *_who())
    queue, people, inbox, drive, state = None, {}, None, None, request.args.get("state")
    if authorize(g.user["role"], li.REVIEW):
        queue = li.queue(conn, *_who(), state=state)
        inbox = li.inbox_folders()
        drive = ds.status(conn)
        found = [r["candidate"] for r in check["items"]] if check else []
        people = _people(conn, [r["patient_id"] for r in queue] + found)
    return render_template("imports.html", progress=progress, queue=queue, people=people, state=state,
                           inbox=inbox, drive=drive, intervals=ds.INTERVALS, check=check, checked=checked,
                           labels=STATE_LABELS, open_states=li.OPEN,
                           name_of=lambda r: Path(r["rel_path"]).name)


@imports_bp.route("/imports")
def index():
    if not authorize(g.user["role"], li.PROGRESS):
        return _refuse("import_progress")
    return _page()


@imports_bp.route("/imports/inbox/check", methods=["POST"])
def check():
    """The dry run from the browser: every file and what staging would make of it. Writes nothing."""
    if not authorize(g.user["role"], li.REVIEW):
        return _refuse("import_check")
    name = request.form.get("folder", "")
    try:
        report = li.dry_run(get_db(), li.inbox_folder(name))
    except li.ImportProblem as e:
        flash(str(e), "danger")
        return redirect(url_for("imports.index"))
    log_audit(get_db(), g.user["username"], g.user["role"], "import_check", "import", allowed=1)
    return _page(check=report, checked=name)


@imports_bp.route("/imports/drive/sync", methods=["POST"])
def drive_sync():
    """Sync now: list the approved Drive folder, fetch what is new, stage it. Attaches nothing."""
    if not authorize(g.user["role"], li.REVIEW):
        return _refuse("drive_sync", "drive")
    try:
        run_id = ds.sync(get_db(), *_who())
    except ds.DriveError as e:
        flash(str(e), "danger")
        return redirect(url_for("imports.index") + "#drive")
    r = ds.run(get_db(), run_id)
    if r["status"] == "error":
        flash(f"The check failed: {r['error_message']}", "danger")
    else:
        flash(f"Drive checked: {r['new_files']} new file(s) staged. Nothing is attached until you confirm.", "success")
    return redirect(url_for("imports.index") + "#drive")


@imports_bp.route("/imports/drive/auto", methods=["POST"])
def drive_auto():
    if not authorize(g.user["role"], li.REVIEW):
        return _refuse("drive_auto", "drive")
    interval = request.form.get("interval", "")
    ds.set_auto(get_db(), request.form.get("enabled") == "1", int(interval) if interval.isdigit() else 0, *_who())
    flash("Automatic check updated.", "success")
    return redirect(url_for("imports.index") + "#drive")


@imports_bp.route("/imports/inbox/stage", methods=["POST"])
def stage():
    """Copy the folder's files into staging and propose an owner for each. Attaches nothing."""
    if not authorize(g.user["role"], li.REVIEW):
        return _refuse("import_stage")
    try:
        batch = li.stage(get_db(), li.inbox_folder(request.form.get("folder", "")), *_who())
    except li.ImportProblem as e:
        flash(str(e), "danger")
        return redirect(url_for("imports.index"))
    flash(f"Run {batch} is staged. Nothing is attached to any patient until you confirm each file.", "success")
    return redirect(url_for("imports.index") + "#queue")


@imports_bp.route("/imports/items/<int:import_id>")
def item(import_id):
    if not authorize(g.user["role"], li.REVIEW):
        return _refuse("import_item_read", f"item:{import_id}")
    conn = get_db()
    try:
        r = li.load_item(conn, import_id, *_who())
    except LookupError:
        abort(404)
    ev = li.evidence(r)
    people = _people(conn, list(ev["candidates"]) + [r["patient_id"]])
    visits = []
    if r["patient_id"]:
        visits = [dict(v) for v in conn.execute("SELECT id, visit_date FROM visits WHERE patient_id = ?"
                                                " ORDER BY visit_date DESC LIMIT 50", (r["patient_id"],))]
    suggested = {v["id"] for v in li.suggested_visits(conn, r, r["patient_id"])}
    return render_template("import_item.html", item=r, ev=ev, people=people, visits=visits,
                           suggested=suggested, labels=STATE_LABELS, strength=STRENGTH,
                           categories=pf.CATEGORIES, open_states=li.OPEN, name=Path(r["rel_path"]).name,
                           previewable=r["kind"] in pf.PREVIEWABLE)


@imports_bp.route("/imports/items/<int:import_id>/original")
def original(import_id):
    if not authorize(g.user["role"], li.REVIEW):
        return _refuse("import_item_read", f"item:{import_id}")
    conn = get_db()
    try:
        r = li.load_item(conn, import_id, *_who())
    except LookupError:
        abort(404)
    data = li.staged_bytes(r)
    if data is None or r["kind"] not in docs.MIMETYPES:
        abort(404)
    resp = Response(data, mimetype=docs.MIMETYPES[r["kind"]])
    if request.args.get("download") or r["kind"] not in pf.PREVIEWABLE:
        resp.headers.set("Content-Disposition", "attachment", filename=docs.display_name(Path(r["rel_path"]).name))
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _decided(import_id, fn, done, **kwargs):
    conn = get_db()
    try:
        fn(conn, import_id, actor=g.user["username"], role=g.user["role"], **kwargs)
    except LookupError:
        abort(404)
    except li.ImportProblem as e:
        flash(str(e), "danger")
        return redirect(url_for("imports.item", import_id=import_id))
    flash(done, "success")
    return redirect(url_for("imports.index") + "#queue")


@imports_bp.route("/imports/items/<int:import_id>/confirm", methods=["POST"])
def confirm(import_id):
    if not authorize(g.user["role"], li.REVIEW):
        return _refuse("import_confirm", f"item:{import_id}")
    visit = request.form.get("visit_id", "")
    return _decided(import_id, li.confirm, "Confirmed and added to the patient's record. It is not shown to"
                    " the patient until you choose to.",
                    patient_key=request.form.get("patient_cf", ""),
                    expected_sha=request.form.get("expected_sha", ""), reason=request.form.get("reason", ""),
                    visit_id=int(visit) if visit.isdigit() else None,
                    category=request.form.get("category") or None)


@imports_bp.route("/imports/items/<int:import_id>/reject", methods=["POST"])
def reject(import_id):
    if not authorize(g.user["role"], li.REVIEW):
        return _refuse("import_reject", f"item:{import_id}")
    return _decided(import_id, li.reject, "Rejected. Nothing was attached; the old file is untouched.",
                    reason=request.form.get("reason", ""))


@imports_bp.route("/imports/items/<int:import_id>/hold", methods=["POST"])
def hold(import_id):
    if not authorize(g.user["role"], li.REVIEW):
        return _refuse("import_hold", f"item:{import_id}")
    return _decided(import_id, li.hold, "Held for investigation.", reason=request.form.get("reason", ""))
