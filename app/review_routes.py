from pathlib import Path

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

import glossary_review
import note_review as nr
from auth import authorize, log_audit

from .db import get_chroma, get_db

review_bp = Blueprint("review", __name__)

SORTED_ROOT = Path("sorted")


def _refuse(action, target=None):
    log_audit(get_db(), g.user["username"], g.user["role"], action, target, allowed=0)
    flash("Only a dentist can review uploaded notes.", "danger")
    return redirect(url_for("dashboard.index"))


def _step(review_id, fn, done, **kwargs):
    try:
        fn(get_db(), review_id, actor=g.user["username"], role=g.user["role"],
           sorted_root=SORTED_ROOT, collection=get_chroma(), **kwargs)
    except LookupError:
        abort(404)
    except nr.ReviewError as e:
        flash(str(e), "danger")
        return redirect(url_for("review.detail", review_id=review_id))
    flash(done, "success")
    return redirect(url_for("review.queue"))


@review_bp.route("/reviews")
def queue():
    if not authorize(g.user["role"], nr.CAPABILITY):
        return _refuse("note_review_read", "queue")
    conn = get_db()
    return render_template("reviews.html", rows=nr.queue(conn), counts=nr.counts(conn))


@review_bp.route("/reviews/<int:review_id>")
def detail(review_id):
    if not authorize(g.user["role"], nr.CAPABILITY):
        return _refuse("note_review_read", f"review:{review_id}")
    try:
        view = nr.detail(get_db(), review_id, g.user["username"], g.user["role"])
    except LookupError:
        abort(404)
    pending, codes = glossary_review.pending_count()
    return render_template("review_detail.html", view=view, pending=pending, codes=codes)


@review_bp.route("/reviews/<int:review_id>/confirm", methods=["POST"])
def confirm(review_id):
    if not authorize(g.user["role"], nr.CAPABILITY):
        return _refuse("note_review_confirm", f"review:{review_id}")
    form = {k: request.form[k] for k in ("visit_date", "procedures", "clinical_notes",
                                         "next_appointment") if k in request.form}
    return _step(review_id, nr.confirm, "Confirmed. The note is now part of the record.", form=form)


@review_bp.route("/reviews/<int:review_id>/reject", methods=["POST"])
def reject(review_id):
    if not authorize(g.user["role"], nr.CAPABILITY):
        return _refuse("note_review_reject", f"review:{review_id}")
    return _step(review_id, nr.reject, "Rejected. Nothing was filed; the upload is kept.",
                 reason=request.form.get("reason", ""))


@review_bp.route("/reviews/<int:review_id>/retry", methods=["POST"])
def retry(review_id):
    if not authorize(g.user["role"], nr.CAPABILITY):
        return _refuse("note_review_retry", f"review:{review_id}")
    return _step(review_id, nr.retry, "Read again. It is waiting for your review.")
