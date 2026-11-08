"""P29 U-010: one page for what patients asked and what waits on staff - appointment requests, imaging work, data
requests - kept apart, each row saying its status, whose it is, its dates and the next step, with a link to where
that step is done. Read only: every action stays on its own page."""
from flask import Blueprint, flash, g, redirect, render_template, url_for

import appointments
import clinic_time
import data_rights
import imaging_bookings
import patient_services
from auth import authorize, log_audit

from .db import get_db
from .i18n import _

requests_bp = Blueprint("requests", __name__)

DATA_KINDS = {"access": "Access", "export": "Copy of data", "amend": "Correction", "erasure": "Erasure"}


@requests_bp.route("/requests")
def index():
    if not authorize(g.user["role"], "manage_appointments"):
        log_audit(get_db(), g.user["username"], g.user["role"], "view_requests", None, allowed=0)
        flash(_("You don't have permission to see requests."), "danger")
        return redirect(url_for("dashboard.index"))
    conn = get_db()
    today = clinic_time.now().date().isoformat()
    asked = []
    for r in appointments.pending_requests(conn):
        day = (r["starts_at"] or "")[:10]
        asked.append({**dict(r), "day": day, "passed": bool(day) and day < today})
    imaging = None
    if authorize(g.user["role"], "view_imaging_requests"):
        imaging = imaging_bookings.tasks(conn, g.user["username"], g.user["role"])
    data = None
    if authorize(g.user["role"], "manage_data_requests"):
        data = [r for r in data_rights.listing(conn) if r["status"] == "open"]
    services = patient_services.waiting(conn) if authorize(g.user["role"], "assign_services") else None
    return render_template("requests.html", asked=asked, imaging=imaging, data=data, data_kinds=DATA_KINDS,
                           services=services)
