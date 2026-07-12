from pathlib import Path

from flask import Flask, g, redirect, request, url_for
from flask_wtf import CSRFProtect

import web_session
from env_config import load_secret_key
from storage import init_db

from . import db

WHITELIST_ENDPOINTS = {"static", "auth.login"}


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = load_secret_key()
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Strict"

    CSRFProtect(app)

    # read db.DB_PATH at call time (not imported by value) so a selftest
    # can point create_app() at a temp db by patching app.db.DB_PATH
    Path(db.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = init_db(db.DB_PATH)
    conn.close()
    db.close_db(app)

    from .auth_routes import auth_bp
    from .dashboard_routes import dashboard_bp
    from .qa_routes import qa_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(qa_bp)

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

    return app
