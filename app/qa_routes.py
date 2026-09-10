import urllib.request

from flask import Blueprint, g, render_template, request

import ask
from auth import authorize, log_audit
from codice_fiscale import is_valid as is_valid_cf
from extract_note import OllamaUnreachable

from .db import get_chroma, get_db

qa_bp = Blueprint("qa", __name__)

_urlopen = urllib.request.urlopen


def answer_question(question, conn, collection, chosen_cf=None):
    # the question is concatenated unchanged into ask.answer_meaning's synthesis
    # prompt, same as the CLI - accepted residual prompt-injection risk given
    # trusted staff, retrieval-bounded context, autoescaped output, no tools
    # "read" names the one patient whose record the answer came from, so the
    # route can audit it; a meaning search reads across notes and names none
    if ask.classify_question(question) == "meaning":
        return {"answer": ask.answer_meaning(question, collection, urlopen=_urlopen), "read": None}

    if chosen_cf:
        if not is_valid_cf(chosen_cf):
            return {"answer": "invalid codice fiscale"}
        return {"answer": ask.answer_exact(chosen_cf, ask.field_for_question(question), conn),
                "read": chosen_cf}

    name = ask.extract_name(question)
    if name is None:
        return {"answer": "couldn't identify a patient in that question"}
    cf = ask.resolve_cf(name, conn)
    if cf is None:
        return {"answer": f"no patient named {name} on record"}
    if isinstance(cf, list):
        return {"candidates": cf, "name": name}
    return {"answer": ask.answer_exact(cf, ask.field_for_question(question), conn), "read": cf}


@qa_bp.route("/qa", methods=["GET", "POST"])
def qa_page():
    if not authorize(g.user["role"], "read_notes"):
        log_audit(get_db(), g.user["username"], g.user["role"], "read_notes", None, allowed=0)
        return render_template(
            "qa.html", error="You don't have permission to view clinical records."
        )

    if request.method == "GET":
        return render_template("qa.html")

    question = request.form.get("question", "").strip()
    chosen_cf = request.form.get("cf")

    try:
        result = answer_question(question, get_db(), get_chroma(), chosen_cf=chosen_cf)
    except OllamaUnreachable as e:
        return render_template("qa.html", question=question, error=str(e))

    # audited before the answer renders, same fail-closed rule as the record
    # view (P02.04). the question text is not logged - it can carry clinical
    # detail - only who asked and whose record answered.
    if "read" in result:
        action = "qa_read" if result["read"] else "qa_search"
        log_audit(get_db(), g.user["username"], g.user["role"], action, result["read"], allowed=1)

    if "candidates" in result:
        return render_template(
            "qa.html", question=question, candidates=result["candidates"], name=result["name"]
        )
    if result["answer"] == "invalid codice fiscale":
        return render_template("qa.html", question=question, error=result["answer"])
    return render_template("qa.html", question=question, answer=result["answer"])
