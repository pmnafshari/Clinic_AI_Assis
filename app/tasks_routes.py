"""P29 NEW-04: tasks for the staff. The Tasks page lists one's own and everyone's open tasks, overdue ones flagged,
and the ones closed in the last 30 days; a task is added there or from a patient's care plan, and marked done or
cancelled by anyone who holds manage_tasks. Each change is audited. Nothing is sent to anyone."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

import patient_id
import staff_tasks
from auth import authorize, log_audit
from codice_fiscale import is_valid as is_valid_cf

from .db import get_db
from .i18n import N_, _

tasks_bp = Blueprint("tasks", __name__)

TASKS = "manage_tasks"
ERRORS = {"say what the task is": N_("Say what the task is."), "choose who does it": N_("Choose who does it."),
          "the due date is not a date": N_("The due date is not a date."), "no such patient": N_("No such patient.")}
NOT_ADDED = N_("That task could not be added.")


def _refused(action):
    log_audit(get_db(), g.user["username"], g.user["role"], action, None, allowed=0)
    flash(_("Tasks are for dentists and reception."), "danger")
    return redirect(url_for("dashboard.index"))


def _back(cf):
    if cf and is_valid_cf(cf):
        return redirect(url_for("patients.detail_view", cf=cf) + "#tasks")
    return redirect(url_for("tasks.index"))


@tasks_bp.route("/tasks")
def index():
    if not authorize(g.user["role"], TASKS):
        return _refused("view_tasks")
    conn = get_db()
    me = g.user["username"]
    everyone = staff_tasks.listing(conn)
    return render_template("tasks.html", mine=[t for t in everyone if t["assignee"] == me],
                           others=[t for t in everyone if t["assignee"] != me],
                           closed=staff_tasks.listing(conn, status="closed"), staff=staff_tasks.staff(conn))


@tasks_bp.route("/tasks", methods=["POST"])
def create():
    if not authorize(g.user["role"], TASKS):
        return _refused("task_create")
    conn = get_db()
    cf = (request.form.get("cf") or "").strip().upper()
    pid = None
    if cf:
        pid = patient_id.resolve(conn, cf) if is_valid_cf(cf) else None
        if pid is None:
            flash(_("No such patient."), "danger")
            return _back(None)
    tid, why = staff_tasks.create(conn, request.form.get("title"), request.form.get("assignee", ""),
                                  request.form.get("day", ""), request.form.get("time", ""), g.user["username"],
                                  pid=pid, note=request.form.get("note"))
    if tid is None:
        flash(_(ERRORS.get(why, NOT_ADDED)), "danger")
        return _back(cf)
    log_audit(conn, g.user["username"], g.user["role"], "task_create", pid, allowed=1, reason=str(tid))
    flash(_("Task added for {who}.", who=request.form.get("assignee")), "success")
    return _back(cf)


@tasks_bp.route("/tasks/<int:tid>/close", methods=["POST"])
def close(tid):
    if not authorize(g.user["role"], TASKS):
        return _refused("task_close")
    conn = get_db()
    status = request.form.get("status", "")
    row = conn.execute("SELECT patient_id FROM staff_tasks WHERE id = ?", (tid,)).fetchone()
    if row is None or not staff_tasks.close(conn, tid, status, g.user["username"]):
        abort(404)
    log_audit(conn, g.user["username"], g.user["role"], "task_" + status, row["patient_id"], allowed=1, reason=str(tid))
    flash(_("Task done.") if status == "done" else _("Task cancelled."), "success")
    cf = request.form.get("cf")
    return _back(cf)
