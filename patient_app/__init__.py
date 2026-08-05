"""the patient-facing Flask app.

a separate app object, process, port, SECRET_KEY and cookie name from the staff
app. it does not import app/, web_auth or web_session - that separation is a
binding security property, not a preference.
"""

from pathlib import Path

from flask import Flask, redirect, render_template, request, send_from_directory, url_for
from flask_wtf import CSRFProtect

import patient_auth
from env_config import load_secret_key

from .strings import DEFAULT_LANGUAGE, LANG_COOKIE_NAME, LANGUAGES, t

# its own secret file, so the two apps never sign each other's cookies
PATIENT_ENV_PATH = Path(".env.patient")

# the vendored bootstrap already on disk for the staff app. served rather than
# copied - offline-first forbids a cdn, and a second copy would drift.
VENDOR_ROOT = Path("app/static/vendor").resolve()


def current_language():
    lang = request.cookies.get(LANG_COOKIE_NAME)
    return lang if lang in LANGUAGES else DEFAULT_LANGUAGE


def create_patient_app(env_path=PATIENT_ENV_PATH):
    app = Flask(__name__)
    app.config["SECRET_KEY"] = load_secret_key(env_path)
    app.config["SESSION_COOKIE_NAME"] = patient_auth.PATIENT_COOKIE_NAME
    # the more exposed surface does not get weaker defaults than the staff app
    CSRFProtect(app)

    @app.context_processor
    def inject_language():
        return {"t": t, "lang": current_language()}

    @app.route("/lang/<code>")
    def set_language(code):
        # redirect to a fixed in-app target, never to request.referrer - that
        # would be an open redirect on an internet-reachable surface
        resp = redirect(url_for("home"))
        if code in LANGUAGES:
            resp.set_cookie(LANG_COOKIE_NAME, code, httponly=False, samesite="Strict")
        return resp

    @app.route("/vendor/<path:filename>")
    def vendor(filename):
        return send_from_directory(VENDOR_ROOT, filename)

    @app.route("/")
    def home():
        # placeholder until 17-04 adds login and the forced pin change
        return render_template("patient_home.html")

    return app
