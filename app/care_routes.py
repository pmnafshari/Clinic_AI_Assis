"""P29 E-002: the care plan on the patient record - the services a patient should attend. A dentist or reception
assigns one and marks it done or takes it off; every change is audited and goes back to the record's Care plan tab.
A referral only: it books nothing and changes no clinical record."""
from flask import Blueprint, abort, flash, g, redirect, request, url_for

import patient_id
import patient_services
from auth import authorize, log_audit
from codice_fiscale import is_valid as is_valid_cf

from .db import get_db
from .i18n import N_, _

care_bp = Blueprint("care", __name__)

SERVICES = "assign_services"
SERVICE_LABELS = {"hygiene": N_("Oral hygiene"), "restorative": N_("Restorative care"), "endodontics": N_("Endodontics"),
                  "surgery": N_("Oral surgery"), "implants": N_("Implants"), "prosthetics": N_("Prosthetics"),
                  "orthodontics": N_("Orthodontics"), "paediatric": N_("Paediatric dentistry"),
                  "radiology": N_("Radiology")}


def _patient(cf):
    if not is_valid_cf(cf):
        abort(404)
    pid = patient_id.resolve(get_db(), cf)
    if pid is None:
        abort(404)
    return pid


def _back(cf, anchor):
    return redirect(url_for("patients.detail_view", cf=cf) + "#" + anchor)


def _refused(cf, action, message):
    log_audit(get_db(), g.user["username"], g.user["role"], action, cf, allowed=0)
    flash(message, "danger")
    return redirect(url_for("patients.detail_view", cf=cf) if is_valid_cf(cf) else url_for("dashboard.index"))


@care_bp.route("/patients/<cf>/services", methods=["POST"])
def assign_service(cf):
    if not authorize(g.user["role"], SERVICES):
        return _refused(cf, "service_assign", _("Services are assigned by a dentist or reception."))
    pid = _patient(cf)
    conn = get_db()
    service = request.form.get("service", "")
    sid, why = patient_services.assign(conn, pid, service, g.user["username"], g.user["role"], request.form.get("note"))
    if sid is None:
        flash(_("Choose a service."), "danger")
        return _back(cf, "services")
    log_audit(conn, g.user["username"], g.user["role"], "service_assign", pid, allowed=1, reason=service)
    flash(_("{service} added to the care plan.", service=_(SERVICE_LABELS[service])), "success")
    return _back(cf, "services")


@care_bp.route("/patients/<cf>/services/<int:sid>/close", methods=["POST"])
def close_service(cf, sid):
    if not authorize(g.user["role"], SERVICES):
        return _refused(cf, "service_close", _("Services are assigned by a dentist or reception."))
    pid = _patient(cf)
    conn = get_db()
    status = request.form.get("status", "")
    if not patient_services.close(conn, pid, sid, status, g.user["username"]):
        abort(404)
    log_audit(conn, g.user["username"], g.user["role"], "service_" + status, pid, allowed=1, reason=str(sid))
    flash(_("Marked done.") if status == "done" else _("Taken off the care plan."), "success")
    return _back(cf, "services")
