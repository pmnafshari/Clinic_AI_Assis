import urllib.request

from flask import Blueprint, flash, g, make_response, redirect, render_template, request, url_for

import agent
import pending_actions
from auth import authorize
from extract_note import OllamaUnreachable

from .db import get_chroma, get_db

agent_bp = Blueprint("agent", __name__)

UNDO_LOG = agent.UNDO_LOG
_urlopen = urllib.request.urlopen


def _confirm_error(message):
    # htmx swaps whatever comes back straight into the modal body, so a
    # fragment caller must get a fragment - agent_confirm.html extends
    # base.html and would nest a whole page inside the modal
    if request.headers.get("HX-Request"):
        return render_template("_confirm_diff.html", error=message)
    return render_template("agent_confirm.html", error=message)


def _build_and_render(call, template, **template_args):
    # no choose_cf passed - the web never prompts, so an ambiguous name comes
    # back as (None, reason) instead of reaching input(). reason is specific
    # (unknown name vs ambiguous vs permissions vs broken tree) so staff see
    # what actually went wrong, not one generic message
    pending, reason = agent.build_pending_action(
        call, get_db(), g.user["role"], g.user["username"]
    )
    if pending is None:
        return render_template(template, error=reason, **template_args)
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
        return _confirm_error("This change has expired. Review and confirm again.")

    # burn the token before applying - the atomic delete makes the token
    # single-use even for concurrent double-submits, and a denied apply
    # below still leaves it consumed
    if not pending_actions.consume_pending_action(get_db(), token, g.user["username"]):
        return _confirm_error("This change has expired. Review and confirm again.")

    # peek at authorize() here only to pick the right response - the actual
    # security boundary is the independent check inside apply_pending_action
    # itself (SC4), which runs either way and is what audits the denial
    allowed = authorize(g.user["role"], pending["tool"])
    try:
        agent.apply_pending_action(
            pending, get_db(), g.user["role"], g.user["username"],
            log_path=UNDO_LOG, collection=get_chroma(),
        )
    except (OSError, ValueError):
        # e.g. the note file vanished during the confirm window
        return _confirm_error("This change can no longer be applied. Review and rebuild it.")

    if not allowed:
        return _confirm_error("You don't have permission to make this change.")

    flash("Change saved.")
    if request.headers.get("HX-Request"):
        # htmx's xhr follows a 302 transparently and would swap the redirect
        # target's whole document into the modal - hand it a client-side
        # redirect back to the patient instead
        resp = make_response("")
        resp.headers["HX-Redirect"] = url_for("patients.detail_view", cf=pending["cf"])
        return resp
    return redirect(url_for("dashboard.index"))


@agent_bp.route("/agent/undo", methods=["POST"])
def undo_change():
    # flash whatever undo_last actually did - a denied undo or an invoice
    # "restore manually" must not read as a completed revert (WR-02)
    status, message = agent.undo_last(
        get_db(), g.user["role"], g.user["username"],
        log_path=UNDO_LOG, collection=get_chroma(),
    )
    flash(message)
    return redirect(url_for("dashboard.index"))
