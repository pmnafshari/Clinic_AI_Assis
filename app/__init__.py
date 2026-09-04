from pathlib import Path

from flask import Flask, flash, g, redirect, request, send_from_directory, url_for
from flask_wtf import CSRFProtect

import web_session
from auth import authorize
from env_config import load_secret_key
from shared import STATIC_ROOT as SHARED_STATIC_ROOT
from storage import init_db

from . import db

# "shared" serves the cross-app token/component css and the font. the login
# page needs it before any session exists, which is the same reason "static"
# is here. named explicitly - a prefix match would open anything starting
# with "s".
WHITELIST_ENDPOINTS = {"static", "shared", "auth.login"}

# reachable while an account still owes a password change - without logout in
# here a flagged user could neither proceed nor leave
CHANGE_PW_ALLOWED = {"static", "shared", "auth.login", "auth.change_password", "auth.logout"}


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = load_secret_key()
    # named explicitly, not left at Flask's default "session" - this app and
    # the patient app share a host, cookies are not port-scoped, and each app
    # signs with its own SECRET_KEY, so leaving both unnamed lets whichever
    # opens second overwrite the other's csrf session cookie (CR-05)
    app.config["SESSION_COOKIE_NAME"] = "staff_csrf"
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
    app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024

    CSRFProtect(app)

    # exposed so _sidebar.html can role-filter nav items server-side
    app.jinja_env.globals["authorize"] = authorize

    # read db.DB_PATH at call time (not imported by value) so a selftest
    # can point create_app() at a temp db by patching app.db.DB_PATH
    Path(db.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = init_db(db.DB_PATH)
    conn.close()
    db.close_db(app)

    from .admin_routes import admin_bp
    from .agent_routes import agent_bp
    from .auth_routes import auth_bp
    from .dashboard_routes import dashboard_bp
    from .notes_routes import notes_bp
    from .patients_routes import patients_bp
    from .qa_routes import qa_bp
    from .reports_routes import reports_bp
    from .upload_routes import upload_bp
    app.register_blueprint(admin_bp)
    app.register_blueprint(agent_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(notes_bp)
    app.register_blueprint(patients_bp)
    app.register_blueprint(qa_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(upload_bp)

    @app.route("/shared/<path:filename>")
    def shared(filename):
        return send_from_directory(SHARED_STATIC_ROOT, filename)

    @app.errorhandler(413)
    def too_large(e):
        # a plain 302 (no explicit 413 status) so the browser follows the
        # redirect and renders the flashed banner instead of Werkzeug's raw
        # error stub (D-12)
        flash("One or more files exceed the 25MB limit. Try uploading fewer or smaller files at once.", "danger")
        if request.headers.get("HX-Request"):
            # the upload form swaps into the toast container, so a redirect
            # here lands a whole page inside the toast stack. ask htmx for a
            # real reload instead - the banner belongs at page level, and the
            # user stays on whichever surface they uploaded from.
            return "", 200, {"HX-Refresh": "true"}
        return redirect(url_for("dashboard.index"))

    @app.before_request
    def require_login():
        if request.endpoint is None:
            return  # unmatched route - let the normal 404 flow run
        if request.endpoint in WHITELIST_ENDPOINTS:
            return
        token = request.cookies.get(web_session.COOKIE_NAME)
        user = web_session.load_session(db.get_db(), token) if token else None
        if user is None:
            return redirect(url_for("auth.login"))
        g.user = user

        if user["must_change_password"] and request.endpoint not in CHANGE_PW_ALLOWED:
            return redirect(url_for("auth.change_password"))

    return app
