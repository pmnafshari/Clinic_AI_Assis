import urllib.request

from flask import Blueprint, g, render_template, request

import ask
from auth import authorize, log_audit
from dental_notes_schema import CF_PATTERN
from extract_note import OllamaUnreachable

from .db import get_chroma, get_db

qa_bp = Blueprint("qa", __name__)

_urlopen = urllib.request.urlopen


def answer_question(question, conn, collection, chosen_cf=None):
    # the question is concatenated unchanged into ask.answer_meaning's synthesis
    # prompt, same as the CLI - accepted residual prompt-injection risk given
    # trusted staff, retrieval-bounded context, autoescaped output, no tools
    if ask.classify_question(question) == "meaning":
        return {"answer": ask.answer_meaning(question, collection, urlopen=_urlopen)}

    if chosen_cf:
        if not CF_PATTERN.match(chosen_cf):
            return {"answer": "invalid codice fiscale"}
        return {"answer": ask.answer_exact(chosen_cf, ask.field_for_question(question), conn)}

    name = ask.extract_name(question)
    if name is None:
        return {"answer": "couldn't identify a patient in that question"}
    cf = ask.resolve_cf(name, conn)
    if cf is None:
        return {"answer": f"no patient named {name} on record"}
    if isinstance(cf, list):
        return {"candidates": cf, "name": name}
    return {"answer": ask.answer_exact(cf, ask.field_for_question(question), conn)}


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

    if "candidates" in result:
        return render_template(
            "qa.html", question=question, candidates=result["candidates"], name=result["name"]
        )
    if result["answer"] == "invalid codice fiscale":
        return render_template("qa.html", question=question, error=result["answer"])
    return render_template("qa.html", question=question, answer=result["answer"])
