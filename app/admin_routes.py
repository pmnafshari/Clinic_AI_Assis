import json

from flask import (
    Blueprint, flash, g, make_response, redirect, render_template, request, url_for
)

import user_admin
from auth import VALID_ROLES, authorize, log_audit

from .db import get_db

admin_bp = Blueprint("admin", __name__)

ACTIONS = ("role", "active", "lockout")


def _denied(action, target=None):
    log_audit(get_db(), g.user["username"], g.user["role"], action, target, allowed=0)


@admin_bp.route("/admin/users")
def users_view():
    if not authorize(g.user["role"], "manage_users"):
        _denied("manage_users")
        flash("You don't have permission to manage staff accounts.", "danger")
        return redirect(url_for("dashboard.index"))

    rows = user_admin.list_users(get_db())
    return render_template("admin_users.html", rows=rows, me=g.user["username"])


@admin_bp.route("/admin/users", methods=["POST"])
def create():
    if not authorize(g.user["role"], "manage_users"):
        _denied("create_user")
        flash("You don't have permission to manage staff accounts.", "danger")
        return redirect(url_for("dashboard.index"))

    ok, message = user_admin.create_user(
        get_db(),
        request.form.get("username", ""),
        request.form.get("role", ""),
        request.form.get("password", ""),
        g.user["username"],
        g.user["role"],
    )
    flash(message, "success" if ok else "danger")
    return redirect(url_for("admin.users_view"))


@admin_bp.route("/admin/users/<username>/confirm")
def confirm_fragment(username):
    # htmx target - a denied fragment returns a bare status, not a redirect
    if not authorize(g.user["role"], "manage_users"):
        _denied("manage_users", username)
        return "", 403

    # the self-edit block has to hold here too, or the modal opens and only
    # fails at submit
    if username == g.user["username"]:
        _denied("manage_users", username)
        return "", 403

    action = request.args.get("action")
    if action not in ACTIONS:
        return "", 400

    row = user_admin.get_user(get_db(), username)
    if row is None:
        return "", 404

    new_role = request.args.get("new_role")
    if action == "role":
        if new_role not in VALID_ROLES or new_role == row["role"]:
            return "", 400
        old, new = row["role"], new_role
    elif action == "active":
        old = "Active" if row["active"] else "Disabled"
        new = "Disabled" if row["active"] else "Active"
    else:
        old, new = "Locked", "Active"

    return render_template(
        "_user_confirm.html",
        username=username,
        action=action,
        old=old,
        new=new,
        new_role=new_role,
        active=0 if row["active"] else 1,
    )


@admin_bp.route("/admin/users/<username>", methods=["POST"])
def apply(username):
    if not authorize(g.user["role"], "manage_users"):
        _denied("manage_users", username)
        return "", 403

    conn = get_db()
    actor, actor_role = g.user["username"], g.user["role"]
    action = request.form.get("action")

    # one dispatch off one derived action, rather than re-reading the form in
    # every branch
    if action == "role":
        ok, message = user_admin.set_role(
            conn, username, request.form.get("new_role", ""), actor, actor_role
        )
    elif action == "active":
        ok, message = user_admin.set_active(
            conn, username, int(request.form.get("active", 0)), actor, actor_role
        )
    elif action == "lockout":
        ok, message = user_admin.clear_lockout(conn, username, actor, actor_role)
    else:
        return "", 400

    row = user_admin.get_user(conn, username)
    if row is None:
        return "", 404

    # the toast rides a trigger header rather than an out-of-band swap: htmx
    # parses a <tr> response inside a table context, and a sibling <div> gets
    # foster-parented out of it, which kills the whole swap
    resp = make_response(render_template("_user_row.html", row=row, me=actor))
    resp.headers["HX-Trigger"] = json.dumps({
        "adminToast": render_template(
            "_user_toast.html", username=username, message=message, ok=ok
        )
    })
    return resp
