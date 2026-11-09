"""P29 NEW-06: the win-back list for reception and the dentist - patients not seen for 12 months or more, with nothing
booked, who agreed to be contacted about offers and news. They are contacted by hand and the contact is recorded here;
nothing is sent from the app. Every record is audited."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

import outreach
import patient_id
from auth import authorize, log_audit
from codice_fiscale import is_valid as is_valid_cf

from .db import get_db
from .i18n import N_, _

outreach_bp = Blueprint("outreach", __name__)

OUTREACH = "handle_outreach"
OUTCOME_LABELS = {"reached": N_("Reached"), "no_answer": N_("No answer"), "not_interested": N_("Not interested"),
                  "booked": N_("Booked a visit")}


def _refused(action):
    log_audit(get_db(), g.user["username"], g.user["role"], action, None, allowed=0)
    flash(_("The win-back list is for reception and dentists."), "danger")
    return redirect(url_for("dashboard.index"))


@outreach_bp.route("/outreach")
def index():
    if not authorize(g.user["role"], OUTREACH):
        return _refused("view_outreach")
    conn = get_db()
    log_audit(conn, g.user["username"], g.user["role"], "view_outreach", None, allowed=1)
    return render_template("outreach.html", rows=outreach.candidates(conn), outcomes=OUTCOME_LABELS)


@outreach_bp.route("/outreach/<cf>/contact", methods=["POST"])
def contact(cf):
    if not authorize(g.user["role"], OUTREACH):
        return _refused("outreach_contact")
    if not is_valid_cf(cf):
        abort(404)
    conn = get_db()
    pid = patient_id.resolve(conn, cf)
    if pid is None:
        abort(404)
    cid, why = outreach.record_contact(conn, pid, request.form.get("outcome", ""), g.user["username"],
                                       request.form.get("note"))
    if cid is None:
        flash(_("Choose how it went.") if why == "choose how it went" else _("This patient has not agreed to be contacted."),
              "danger")
    else:
        log_audit(conn, g.user["username"], g.user["role"], "outreach_contact", pid, allowed=1,
                  reason=request.form.get("outcome"))
        flash(_("Contact recorded."), "success")
    return redirect(url_for("outreach.index"))
