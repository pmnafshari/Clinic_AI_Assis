import urllib.request
from pathlib import Path

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from auth import authorize, log_audit
from dental_notes_schema import DentalNote
from extract_note import OllamaUnreachable, call_model, parse_reply
from storage import get_collection, save_new_note

from .db import get_db

notes_bp = Blueprint("notes", __name__)

CHROMA_PATH = "db/chroma"
SORTED_ROOT = Path("sorted")
_urlopen = urllib.request.urlopen

_collection_cache = None


def _collection():
    # app-lifetime cache, not per-request g - chroma client is thread-safe
    global _collection_cache
    if _collection_cache is None:
        _collection_cache = get_collection(CHROMA_PATH)
    return _collection_cache


def extract_note(raw_note):
    # composes extract_note.py's call_model + parse_reply with the local
    # _urlopen seam (same shape as qa_routes.CHROMA_PATH/_urlopen), since
    # extract_note.py's own extract_note() has no urlopen override point
    return parse_reply(call_model(raw_note, urlopen=_urlopen))


@notes_bp.route("/notes/new", methods=["GET", "POST"])
def new_note():
    if request.method == "GET":
        return render_template("notes_new.html")

    if "raw_note" in request.form:
        # step 1: extract once, render editable preview (D-02)
        try:
            note = extract_note(request.form["raw_note"])
        except OllamaUnreachable as e:
            return render_template("notes_new.html", error=str(e))
        except ValueError as e:
            return render_template("notes_new.html", error=f"extraction rejected: {e}")
        return render_template("notes_new.html", preview=note)

    # step 2: confirm POST - re-validate the (possibly staff-corrected) fields,
    # never re-call extract_note (D-04's "no re-derivation" applies here too)
    try:
        note = DentalNote(
            patient_name=request.form["patient_name"],
            codice_fiscale=request.form["codice_fiscale"],
            phone=request.form.get("phone") or None,
            clinical_notes=request.form.get("clinical_notes", ""),
            procedures=[p.strip() for p in request.form.get("procedures", "").split(",") if p.strip()],
            next_appointment=request.form.get("next_appointment") or None,
        )
    except Exception as e:
        return render_template("notes_new.html", error=f"invalid fields: {e}")

    if not authorize(g.user["role"], "append_note"):
        log_audit(get_db(), g.user["username"], g.user["role"], "append_note",
                   target=note.codice_fiscale, allowed=0)
        return render_template("notes_new.html", error="You don't have permission to add notes.")

    save_new_note(note, get_db(), _collection(), g.user["role"], g.user["username"], sorted_root=SORTED_ROOT)
    flash("Note saved.")
    return redirect(url_for("dashboard.index"))
