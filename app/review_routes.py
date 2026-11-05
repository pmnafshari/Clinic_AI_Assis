from pathlib import Path

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

import glossary_review
from dental_notes_schema import unsupported_procedures
import note_review as nr
from auth import authorize, log_audit

from . import reasons
from .db import get_chroma, get_db
from .i18n import _

review_bp = Blueprint("review", __name__)

SORTED_ROOT = Path("sorted")


def _refuse(action, target=None):
    log_audit(get_db(), g.user["username"], g.user["role"], action, target, allowed=0)
    flash(_("Only a dentist can review uploaded notes."), "danger")
    return redirect(url_for("dashboard.index"))


def _step(review_id, fn, done, kind="success", **kwargs):
    try:
        fn(get_db(), review_id, actor=g.user["username"], role=g.user["role"],
           sorted_root=SORTED_ROOT, collection=get_chroma(), **kwargs)
    except LookupError:
        abort(404)
    except nr.ReviewError as e:
        flash(str(e), "danger")
        return redirect(url_for("review.detail", review_id=review_id))
    flash(done, kind)
    return redirect(url_for("review.queue"))


@review_bp.route("/reviews")
def queue():
    if not authorize(g.user["role"], nr.CAPABILITY):
        return _refuse("note_review_read", "queue")
    conn = get_db()
    return render_template("reviews.html", rows=nr.queue(conn), counts=nr.counts(conn),
                           decided=nr.recent_decisions(conn))


@review_bp.route("/reviews/<int:review_id>")
def detail(review_id):
    if not authorize(g.user["role"], nr.CAPABILITY):
        return _refuse("note_review_read", f"review:{review_id}")
    try:
        view = nr.detail(get_db(), review_id, g.user["username"], g.user["role"])
    except LookupError:
        abort(404)
    pending, codes = glossary_review.pending_count()
    # P29 F-018: a treatment the model wrote that the original does not say is left out of the draft, and named
    x = view["extraction"]
    unsaid = unsupported_procedures(x.get("procedures") or [], view["original"]) if x and view["original"] else []
    return render_template("review_detail.html", view=view, pending=pending, codes=codes, unsaid=unsaid)


@review_bp.route("/reviews/<int:review_id>/confirm", methods=["POST"])
def confirm(review_id):
    if not authorize(g.user["role"], nr.CAPABILITY):
        return _refuse("note_review_confirm", f"review:{review_id}")
    form = {k: request.form[k] for k in ("visit_date", "procedures", "clinical_notes",
                                         "next_appointment", "phone") if k in request.form}
    # P29 F-036: say what the confirm actually does - an upload becomes a new visit, an
    # earlier note was already in the record and is only marked as reviewed
    row = get_db().execute("SELECT n.origin, p.patient_name FROM note_reviews n LEFT JOIN patients p"
                           " ON p.patient_id = n.patient_id WHERE n.id = ?", (review_id,)).fetchone()
    if row is not None and row["origin"] == "legacy":
        done = _("Marked as reviewed. This earlier note was already in the record - no new visit was made.")
    else:
        who = row["patient_name"] if row is not None and row["patient_name"] else _("the patient")
        done = _("Confirmed. A new visit is filed for {who}.", who=who)
    return _step(review_id, nr.confirm, done, form=form)


@review_bp.route("/reviews/<int:review_id>/reject", methods=["POST"])
def reject(review_id):
    if not authorize(g.user["role"], nr.CAPABILITY):
        return _refuse("note_review_reject", f"review:{review_id}")
    return _step(review_id, nr.reject, _("Rejected. Nothing was filed; the upload is kept."), kind="removed",
                 reason=reasons.chosen(request.form, reasons.REJECT_NOTE))


@review_bp.route("/reviews/<int:review_id>/retry", methods=["POST"])
def retry(review_id):
    if not authorize(g.user["role"], nr.CAPABILITY):
        return _refuse("note_review_retry", f"review:{review_id}")
    return _step(review_id, nr.retry, _("Read again. It is waiting for your review."))
