from flask import Blueprint, flash, g, redirect, render_template, request, url_for

import reminder_job
import reminders
from auth import authorize, log_audit

from .db import get_db

reminders_bp = Blueprint("reminders", __name__)


def _refuse(action, target=None):
    log_audit(get_db(), g.user["username"], g.user["role"], action, target, allowed=0)
    flash("You don't have permission to see reminders.", "danger")
    return redirect(url_for("dashboard.index"))


@reminders_bp.route("/reminders")
def index():
    if not authorize(g.user["role"], "view_reminders"):
        return _refuse("view_reminders")
    conn = get_db()
    counts, jobs = reminders.dashboard(conn)
    status = request.args.get("status")
    if status in counts:
        jobs = [j for j in jobs if j["status"] == status]
    return render_template("reminders.html", counts=counts, jobs=jobs, status=status,
                           last_run=reminders.last_run(conn),
                           schedule=reminder_job.SCHEDULE,
                           sending_off=reminders.DisabledTransport.reason,
                           can_retry=authorize(g.user["role"], "retry_reminder"))


@reminders_bp.route("/reminders/<int:job_id>/retry", methods=["POST"])
def retry(job_id):
    if not authorize(g.user["role"], "retry_reminder"):
        return _refuse("retry_reminder", f"reminder:{job_id}")
    try:
        done = reminders.retry(get_db(), job_id, g.user["username"], g.user["role"])
    except PermissionError:
        return _refuse("retry_reminder", f"reminder:{job_id}")
    flash("Back in the queue. Nothing is sent until a provider is configured." if done
          else "That reminder is not in a state that can be retried.",
          "success" if done else "danger")
    return redirect(url_for("reminders.index", status=request.form.get("status") or None))
