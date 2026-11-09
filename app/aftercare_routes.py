"""P29 R-020 / NEW-05: the dentist's aftercare page. Each treatment's text in Italian and English: the approved one
patients see, the draft waiting, a form to write a new draft, and Approve. The shipped texts are demo samples, marked
so; none is approved until a dentist approves it here. Every change is audited."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

import aftercare
from auth import authorize, log_audit

from .db import get_db
from .i18n import N_, _

aftercare_bp = Blueprint("aftercare", __name__)

MANAGE = "manage_aftercare"
TOPIC_LABELS = {"extraction": N_("Extraction"), "filling": N_("Filling"), "rct": N_("Root canal treatment"),
                "crown": N_("Crown"), "implant": N_("Implant"), "cleaning": N_("Professional cleaning"),
                "perio": N_("Gum treatment")}
LANG_LABELS = {"it": N_("Italian"), "en": N_("English")}


def _refused(action):
    log_audit(get_db(), g.user["username"], g.user["role"], action, None, allowed=0)
    flash(_("Aftercare texts are written and approved by a dentist."), "danger")
    return redirect(url_for("dashboard.index"))


@aftercare_bp.route("/aftercare")
def index():
    if not authorize(g.user["role"], MANAGE):
        return _refused("view_aftercare")
    return render_template("aftercare.html", rows=aftercare.overview(get_db()))


@aftercare_bp.route("/aftercare/<topic>/<lang>", methods=["POST"])
def save(topic, lang):
    if not authorize(g.user["role"], MANAGE):
        return _refused("aftercare_draft")
    conn = get_db()
    tid, why = aftercare.save_draft(conn, topic, lang, request.form.get("body"), g.user["username"])
    if tid is None:
        if why == "unknown topic":
            abort(404)
        flash(_("Write the text first."), "danger")
    else:
        log_audit(conn, g.user["username"], g.user["role"], "aftercare_draft", None, allowed=1, reason=f"{topic} {lang}")
        flash(_("Draft saved. Patients see it once you approve it."), "success")
    return redirect(url_for("aftercare.index") + f"#topic-{topic}")


@aftercare_bp.route("/aftercare/texts/<int:text_id>/approve", methods=["POST"])
def approve(text_id):
    if not authorize(g.user["role"], MANAGE):
        return _refused("aftercare_approve")
    conn = get_db()
    row = conn.execute("SELECT topic, lang FROM aftercare_texts WHERE id = ?", (text_id,)).fetchone()
    if row is None or not aftercare.approve(conn, text_id, g.user["username"]):
        abort(404)
    log_audit(conn, g.user["username"], g.user["role"], "aftercare_approve", None, allowed=1,
              reason=f"{row['topic']} {row['lang']} {text_id}")
    flash(_("Approved. Patients with this treatment in the last 30 days now see it."), "success")
    return redirect(url_for("aftercare.index") + f"#topic-{row['topic']}")
