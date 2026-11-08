"""P29 NEW-07 / E-008: the dental chart's actions. Reading it is on the patient record (read_clinical); every change
- approving or refusing what the notes propose, adding a mark, taking one off - is edit_dental_chart, the dentist's
alone. Each change is audited and goes back to the record's chart tab."""
from flask import Blueprint, abort, flash, g, redirect, request, url_for

import dental_chart
import patient_id
from auth import authorize, log_audit
from codice_fiscale import is_valid as is_valid_cf

from .db import get_db
from .i18n import N_, _

chart_bp = Blueprint("chart", __name__)

EDIT = "edit_dental_chart"

# how the chart names each kind of work, its states and the teeth's places (FDI)
KIND_LABELS = {
    "caries": N_("Caries"), "filling": N_("Filling"), "temporary": N_("Temporary filling"),
    "sealant": N_("Sealant"), "inlay": N_("Inlay or onlay"), "veneer": N_("Veneer"), "crown": N_("Crown"),
    "bridge": N_("Bridge"), "denture": N_("Removable denture"), "rct": N_("Root canal treatment"),
    "post": N_("Post and core"), "implant": N_("Implant"), "extraction": N_("Extraction"),
    "missing": N_("Missing tooth"), "fracture": N_("Fracture"), "perio": N_("Periodontal treatment"),
    "impacted": N_("Impacted tooth"), "other": N_("Other work"),
}
STATE_LABELS = {"done": N_("Done or present"), "planned": N_("Planned")}
SURFACE_LABELS = {"mesial": N_("mesial"), "occlusal": N_("occlusal"), "incisal": N_("incisal"),
                  "distal": N_("distal"), "buccal": N_("buccal"), "lingual": N_("lingual"),
                  "palatal": N_("palatal")}
QUADRANTS = {1: N_("upper right"), 2: N_("upper left"), 3: N_("lower left"), 4: N_("lower right")}
POSITIONS = {1: N_("central incisor"), 2: N_("lateral incisor"), 3: N_("canine"), 4: N_("first premolar"),
             5: N_("second premolar"), 6: N_("first molar"), 7: N_("second molar"), 8: N_("third molar")}
BABY_POSITIONS = {1: N_("central incisor"), 2: N_("lateral incisor"), 3: N_("canine"), 4: N_("first molar"),
                  5: N_("second molar")}
# the five surfaces as the form offers them: the letter stored, and what it is called on most teeth
SURFACE_CHOICES = [("M", N_("Mesial (M)")), ("O", N_("Occlusal or incisal (O/I)")), ("D", N_("Distal (D)")),
                   ("B", N_("Buccal or vestibular (B/V)")), ("L", N_("Lingual or palatal (L/P)"))]
ERRORS = {"that is not a tooth number": N_("That is not a tooth number."), "choose the work": N_("Choose the work."),
          "choose done or planned": N_("Choose done or planned."), "the date is not a date": N_("The date is not a date."),
          "say what the other work is": N_("Say what the other work is in the note.")}
NOT_ADDED = N_("That mark could not be added.")


def tooth_name(tooth):
    """'first molar, lower right' in the page's language; a baby tooth says so"""
    quadrant, place = tooth // 10, tooth % 10
    if quadrant >= 5:
        return _("{position}, {side} (baby tooth)", position=_(BABY_POSITIONS[place]), side=_(QUADRANTS[quadrant - 4]))
    return _("{position}, {side}", position=_(POSITIONS[place]), side=_(QUADRANTS[quadrant]))


def describe(mark):
    """one line for a mark: the work, its surfaces, its state"""
    words = _(KIND_LABELS.get(mark["kind"], "Other work"))
    if mark["surface_names"]:
        words += " (" + ", ".join(_(SURFACE_LABELS[s]) for s in mark["surface_names"]) + ")"
    return words + " · " + _(STATE_LABELS.get(mark["state"], "Done or present"))


def tooth_label(t):
    """what a screen reader and the tooltip say for a drawn tooth: its number and place, then each mark on it"""
    head = _("Tooth {n}, {name}", n=t["tooth"], name=tooth_name(t["tooth"]))
    if not t["marks"]:
        return head + ": " + _("nothing marked")
    return head + ": " + "; ".join(describe(m) for m in t["marks"])


def _patient(cf):
    # the same checks as the record itself: a real codice fiscale, a patient behind it
    if not is_valid_cf(cf):
        abort(404)
    pid = patient_id.resolve(get_db(), cf)
    if pid is None:
        abort(404)
    return pid


def _back(cf):
    return redirect(url_for("patients.detail_view", cf=cf) + "#chart")


def _refused(cf, action):
    log_audit(get_db(), g.user["username"], g.user["role"], action, cf, allowed=0)
    flash(_("Only a dentist changes the dental chart."), "danger")
    return redirect(url_for("patients.detail_view", cf=cf) if is_valid_cf(cf) else url_for("dashboard.index"))


@chart_bp.route("/patients/<cf>/chart/decide", methods=["POST"])
def decide(cf):
    """the popup's answer: approve or refuse each proposal, or all of them at once"""
    if not authorize(g.user["role"], EDIT):
        return _refused(cf, "dental_chart_decide")
    pid = _patient(cf)
    conn = get_db()
    everything = request.form.get("all")
    approved = refused = 0
    for mark in dental_chart.pending(conn, pid):
        choice = everything if everything in ("approve", "reject") else request.form.get(f"decision-{mark['id']}")
        if choice not in ("approve", "reject"):
            continue
        if dental_chart.decide(conn, pid, mark["id"], choice == "approve", g.user["username"]):
            if choice == "approve":
                approved += 1
            else:
                refused += 1
            log_audit(conn, g.user["username"], g.user["role"], "dental_chart_" + choice, pid, allowed=1,
                      reason=f"{mark['tooth']} {mark['kind']}")
    if approved or refused:
        flash(_("Dental chart: {approved} approved, {refused} refused.", approved=approved, refused=refused), "success")
    else:
        flash(_("Nothing was decided: choose approve or refuse for a suggestion."), "warning")
    return _back(cf)


@chart_bp.route("/patients/<cf>/chart/marks", methods=["POST"])
def add(cf):
    if not authorize(g.user["role"], EDIT):
        return _refused(cf, "dental_chart_add")
    pid = _patient(cf)
    conn = get_db()
    try:
        tooth = int(request.form.get("tooth", ""))
    except ValueError:
        tooth = 0
    kind = request.form.get("kind", "")
    mark_id, why = dental_chart.add(conn, pid, tooth, kind, "".join(request.form.getlist("surface")),
                                    request.form.get("state", ""), g.user["username"],
                                    request.form.get("marked_on"), request.form.get("note"))
    if mark_id is None:
        flash(_(ERRORS.get(why, NOT_ADDED)), "danger")
        return _back(cf)
    log_audit(conn, g.user["username"], g.user["role"], "dental_chart_add", pid, allowed=1, reason=f"{tooth} {kind}")
    flash(_("Tooth {tooth}: {work} added to the chart.", tooth=tooth, work=_(KIND_LABELS[kind])), "success")
    return _back(cf)


@chart_bp.route("/patients/<cf>/chart/marks/<int:mark_id>/remove", methods=["POST"])
def remove(cf, mark_id):
    if not authorize(g.user["role"], EDIT):
        return _refused(cf, "dental_chart_remove")
    pid = _patient(cf)
    conn = get_db()
    if not dental_chart.remove(conn, pid, mark_id, g.user["username"]):
        abort(404)
    log_audit(conn, g.user["username"], g.user["role"], "dental_chart_remove", pid, allowed=1, reason=str(mark_id))
    flash(_("The mark was taken off the chart."), "success")
    return _back(cf)
