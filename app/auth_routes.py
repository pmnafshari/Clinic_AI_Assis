from flask import Blueprint, g, redirect, render_template, request, url_for

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


@auth_bp.route("/logout", methods=["POST"])
def logout():
    token = request.cookies.get(web_session.COOKIE_NAME)
    if token:
        web_session.destroy_session(get_db(), token)
    log_audit(get_db(), g.user["username"], g.user["role"], "logout", None, allowed=1)
    resp = redirect(url_for("auth.login"))
    resp.delete_cookie(web_session.COOKIE_NAME)
    return resp
