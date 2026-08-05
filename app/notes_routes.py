import urllib.request
from pathlib import Path

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from auth import authorize, log_audit
from dental_notes_schema import CF_PATTERN, DentalNote
from extract_note import OllamaUnreachable, call_model, parse_reply
from storage import lookup_patient, save_new_note

from .db import get_chroma, get_db

notes_bp = Blueprint("notes", __name__)

SORTED_ROOT = Path("sorted")
_urlopen = urllib.request.urlopen


def extract_note(raw_note, fallback_cf=None):
    # composes extract_note.py's call_model + parse_reply with the local
    # _urlopen seam (same shape as qa_routes._urlopen), since extract_note.py's
    # own extract_note() has no urlopen override point
    return parse_reply(call_model(raw_note, urlopen=_urlopen), fallback_cf=fallback_cf)


def _locked_patient(cf):
    # resolved once, used by GET and both POST steps, so the note form can
    # never be tricked into filing under a patient the request didn't lock
    if not cf:
        return None
    if not CF_PATTERN.match(cf):
        abort(404)
    patient = lookup_patient(cf, get_db())
    if patient is None:
        abort(404)
    return {"cf": cf, "patient_name": patient["patient_name"]}


@notes_bp.route("/notes/new", methods=["GET", "POST"])
def new_note():
    if request.method == "GET":
        cf = request.args.get("cf", "")
        if cf:
            # ?cf= turns an empty form into a patient-name disclosure - gate
            # it on append_note same as the write itself, the no-cf GET stays
            # ungated
            if not authorize(g.user["role"], "append_note"):
                log_audit(get_db(), g.user["username"], g.user["role"], "append_note",
                           target=cf, allowed=0)
                flash("You don't have permission to add notes.", "danger")
                return redirect(url_for("dashboard.index"))
        return render_template("notes_new.html", locked=_locked_patient(cf))

    if "raw_note" in request.form:
        # step 1: extract once, render editable preview (D-02)
        cf = request.form.get("cf", "")
        locked = _locked_patient(cf)
        try:
            # a locked request already knows the cf, so a note that doesn't
            # spell one out is still extractable (the save path uses the
            # locked value regardless)
            note = extract_note(
                request.form["raw_note"], fallback_cf=locked["cf"] if locked else None
            )
        except OllamaUnreachable as e:
            return render_template("notes_new.html", error=str(e), locked=locked)
        except ValueError as e:
            return render_template("notes_new.html", error=f"extraction rejected: {e}", locked=locked)

        # D-03: extraction misreading a name shouldn't block the note - warn
        # and let the human decide, the locked identity still wins on save
        mismatch = None
        if locked is not None:
            cf_differs = note.codice_fiscale != locked["cf"]
            name_differs = (
                note.patient_name.strip().casefold() != locked["patient_name"].strip().casefold()
            )
            if cf_differs or name_differs:
                # only quote a cf the model actually read - when it read none we
                # filled in the locked one, and printing that back would look
                # like the note agreed on the patient when it never said
                named = f"{note.patient_name} ({note.codice_fiscale})" if cf_differs \
                    else note.patient_name
                mismatch = (
                    f"this note names {named} - "
                    f"it will be filed under {locked['patient_name']} ({locked['cf']})"
                )
        return render_template("notes_new.html", preview=note, locked=locked, mismatch=mismatch)

    # step 2: confirm POST - re-validate the (possibly staff-corrected) fields,
    # never re-call extract_note (D-04's "no re-derivation" applies here too)
    cf = request.form.get("cf", "")
    locked = _locked_patient(cf)
    try:
        invoices = [
            {"amount": amount, "description": description}
            for amount, description in zip(
                request.form.getlist("invoice_amount"),
                request.form.getlist("invoice_description"),
            )
        ]
        # D-02: a locked identity is never read from the form - a tampered
        # patient_name/codice_fiscale in the confirm POST has no path in
        if locked is not None:
            patient_name = locked["patient_name"]
            codice_fiscale = locked["cf"]
        else:
            patient_name = request.form["patient_name"]
            codice_fiscale = request.form["codice_fiscale"]
        note = DentalNote(
            patient_name=patient_name,
            codice_fiscale=codice_fiscale,
            phone=request.form.get("phone") or None,
            visit_date=request.form.get("visit_date") or None,
            clinical_notes=request.form.get("clinical_notes", ""),
            procedures=[p.strip() for p in request.form.get("procedures", "").split(",") if p.strip()],
            invoices=invoices,
            next_appointment=request.form.get("next_appointment") or None,
        )
    except Exception as e:
        return render_template("notes_new.html", error=f"invalid fields: {e}", locked=locked)

    if not authorize(g.user["role"], "append_note"):
        log_audit(get_db(), g.user["username"], g.user["role"], "append_note",
                   target=note.codice_fiscale, allowed=0)
        return render_template(
            "notes_new.html", error="You don't have permission to add notes.", locked=locked
        )

    status = save_new_note(
        note, get_db(), get_chroma(), g.user["role"], g.user["username"], sorted_root=SORTED_ROOT
    )
    if status == "failed":
        flash("Note saved to disk but it is not searchable yet - ask an admin to run a backfill.", "danger")
    else:
        flash("Note saved.")

    # D-04: both outcomes return to the patient screen when scoped to one -
    # the json is already on disk, so keeping staff on the form risks a
    # duplicate note on retry
    if locked is not None:
        return redirect(url_for("patients.detail_view", cf=locked["cf"]))
    return redirect(url_for("dashboard.index"))
