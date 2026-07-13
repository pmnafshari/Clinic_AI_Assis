import urllib.request

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

import agent
import pending_actions
from auth import authorize
from extract_note import OllamaUnreachable
from storage import get_collection

from .db import get_db

agent_bp = Blueprint("agent", __name__)

CHROMA_PATH = "db/chroma"
UNDO_LOG = agent.UNDO_LOG
_urlopen = urllib.request.urlopen

_collection_cache = None


def _collection():
    # app-lifetime cache, not per-request g - chroma client is thread-safe
    global _collection_cache
    if _collection_cache is None:
        _collection_cache = get_collection(CHROMA_PATH)
    return _collection_cache


def _build_and_render(call, template, **template_args):
    # the web never prompts for a codice fiscale - the chooser just records
    # that the name was ambiguous so the page can say so instead of hanging
    # on input() or showing the generic error
    ambiguous = []

    def never_pick(candidates):
        ambiguous.append(candidates)
        return ""

    pending = agent.build_pending_action(
        call, get_db(), g.user["role"], g.user["username"], choose_cf=never_pick
    )
    if pending is None:
        if ambiguous:
            error = "Multiple patients match that name - be more specific."
        else:
            error = "Couldn't build that change - check the patient name and your permissions."
        return render_template(template, error=error, **template_args)
    token = pending_actions.create_pending_action(
        get_db(), g.user["username"], g.user["role"], pending
    )
    return render_template("agent_confirm.html", diff_line=pending["diff_line"], token=token)


@agent_bp.route("/agent/command", methods=["GET", "POST"])
def command_page():
    if request.method == "GET":
        return render_template("agent_command.html")

    command = request.form.get("command", "")
    try:
        reply = agent.call_model(command, _urlopen)
    except OllamaUnreachable as e:
        return render_template("agent_command.html", command=command, error=str(e))

    try:
        call = agent.parse_tool_call(reply)
    except ValueError as e:
        return render_template(
            "agent_command.html", command=command,
            error=f"couldn't understand that command: {e}",
        )

    return _build_and_render(call, "agent_command.html", command=command)


@agent_bp.route("/agent/edit", methods=["GET", "POST"])
def edit_page():
    fields = sorted(agent.EDITABLE_FIELDS)
    if request.method == "GET":
        return render_template("agent_edit.html", fields=fields)

    patient = request.form.get("patient", "")
    field = request.form.get("field", "")
    value = request.form.get("value", "")

    try:
        call = agent.ToolCall(tool="update_field", args={"patient": patient, "field": field, "value": value})
        call.parsed_args()
    except Exception as e:
        return render_template(
            "agent_edit.html", fields=fields, patient=patient, field=field, value=value,
            error=f"invalid fields: {e}",
        )

    return _build_and_render(call, "agent_edit.html", fields=fields, patient=patient, field=field, value=value)


@agent_bp.route("/agent/confirm", methods=["POST"])
def confirm_change():
    token = request.form.get("token", "")
    pending = pending_actions.load_pending_action(get_db(), token, g.user["username"])
    if pending is None:
        return render_template(
            "agent_confirm.html",
            error="This change has expired. Review and confirm again.",
        )

    # burn the token before applying - the atomic delete makes the token
    # single-use even for concurrent double-submits, and a denied apply
    # below still leaves it consumed
    if not pending_actions.consume_pending_action(get_db(), token, g.user["username"]):
        return render_template(
            "agent_confirm.html",
            error="This change has expired. Review and confirm again.",
        )

    # peek at authorize() here only to pick the right response - the actual
    # security boundary is the independent check inside apply_pending_action
    # itself (SC4), which runs either way and is what audits the denial
    allowed = authorize(g.user["role"], pending["tool"])
    try:
        agent.apply_pending_action(
            pending, get_db(), g.user["role"], g.user["username"],
            log_path=UNDO_LOG, collection=_collection(),
        )
    except (OSError, ValueError):
        # e.g. the note file vanished during the confirm window
        return render_template(
            "agent_confirm.html",
            error="This change can no longer be applied. Review and rebuild it.",
        )

    if not allowed:
        return render_template(
            "agent_confirm.html", error="You don't have permission to make this change."
        )

    flash("Change saved.")
    return redirect(url_for("dashboard.index"))


@agent_bp.route("/agent/undo", methods=["POST"])
def undo_change():
    agent.undo_last(
        get_db(), g.user["role"], g.user["username"],
        log_path=UNDO_LOG, collection=_collection(),
    )
    flash("Undo processed.")
    return redirect(url_for("dashboard.index"))
