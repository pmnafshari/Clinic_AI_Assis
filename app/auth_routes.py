from flask import Blueprint, g, redirect, render_template, request, url_for
from werkzeug.security import generate_password_hash

import web_auth
import web_session
from auth import log_audit

from .db import get_db

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    username = request.form.get("username", "")
    password = request.form.get("password", "")

    role = web_auth.attempt_login(username, password, get_db())
    if role is None:
        return render_template("login.html", error="invalid username or password")

    token = web_session.create_session(get_db(), username, role)
    resp = redirect(url_for("dashboard.index"))
    resp.set_cookie(
        web_session.COOKIE_NAME, token,
        httponly=True, samesite="Strict", max_age=None,
    )
    return resp


@auth_bp.route("/change-password", methods=["GET", "POST"])
def change_password():
    min_length = web_auth.MIN_PASSWORD_LENGTH

    if request.method == "GET":
        return render_template("change_password.html", min_length=min_length)

    current = request.form.get("current", "")
    password = request.form.get("password", "")
    confirm = request.form.get("confirm", "")

    def refuse(message):
        return render_template("change_password.html", error=message, min_length=min_length)

    # cheap validations first, the credential check last - otherwise a typo in
    # confirm or a short new password burns a failed attempt against the
    # lockout and staff get locked out over mistakes in unrelated fields
    if not current:
        return refuse("Enter your current password.")
    if not password:
        return refuse("Enter a new password.")
    if password != confirm:
        return refuse("The two passwords don't match.")
    if len(password) < min_length:
        return refuse(f"New password must be at least {min_length} characters.")
    if password == current:
        return refuse("New password must be different from your current one.")

    conn = get_db()
    # one message for a wrong password and for a locked account - telling them
    # apart would confirm to an attacker that the password itself was right
    if web_auth.verify_current_password(g.user["username"], current, conn) is None:
        return refuse("Current password is not correct.")

    conn.execute(
        "UPDATE users SET password_hash = ?, must_change_password = 0 WHERE username = ?",
        (generate_password_hash(password), g.user["username"]),
    )
    conn.commit()
    log_audit(conn, g.user["username"], g.user["role"], "change_password",
              g.user["username"], allowed=1)

    # every session for this account, including this one (D-03) - matches what
    # disabling an account already does
    web_session.destroy_user_sessions(conn, g.user["username"])
    resp = redirect(url_for("auth.login"))
    resp.delete_cookie(web_session.COOKIE_NAME)
    return resp


@auth_bp.route("/logout", methods=["POST"])
def logout():
    token = request.cookies.get(web_session.COOKIE_NAME)
    if token:
        web_session.destroy_session(get_db(), token)
    log_audit(get_db(), g.user["username"], g.user["role"], "logout", None, allowed=1)
    resp = redirect(url_for("auth.login"))
    resp.delete_cookie(web_session.COOKIE_NAME)
    return resp
