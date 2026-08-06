"""the patient-facing Flask app.

a separate app object, process, port, SECRET_KEY and cookie name from the staff
app. it does not import app/, web_auth or web_session - that separation is a
binding security property, not a preference.
"""

from pathlib import Path

from flask import Flask, g, redirect, render_template, send_from_directory, url_for
from flask_wtf import CSRFProtect

import patient_auth
import storage
from env_config import load_secret_key

from . import routes
from .routes import patient_bp, require_patient_session
from .strings import LANG_COOKIE_NAME, LANGUAGES, current_language, t

# its own secret file, so the two apps never sign each other's cookies
PATIENT_ENV_PATH = Path(".env.patient")

# the vendored bootstrap already on disk for the staff app. served rather than
# copied - offline-first forbids a cdn, and a second copy would drift.
VENDOR_ROOT = Path("app/static/vendor").resolve()


def create_patient_app(env_path=PATIENT_ENV_PATH):
    app = Flask(__name__)
    app.config["SECRET_KEY"] = load_secret_key(env_path)
    # Flask's own session cookie (flask_wtf's CSRF token storage) gets an
    # explicit name for two reasons. it must not alias
    # patient_auth.PATIENT_COOKIE_NAME, or the two would silently overwrite
    # each other's Set-Cookie header and patients would get logged out the
    # next time a csrf token regenerates. and it must not stay at Flask's
    # default "session" either - this app and the staff app share a host,
    # RFC 6265 cookies are not scoped by port, and each app signs with its
    # own SECRET_KEY, so leaving both at "session" means whichever app is
    # opened second overwrites a cookie the other one signed. the victim's
    # next write then 400s with no explanation (CR-05)
    app.config["SESSION_COOKIE_NAME"] = "patient_csrf"
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
    # one constant drives Secure on both this cookie and the auth cookie in
    # routes.py, so a selftest that flips it flips both writers at once (CR-06)
    app.config["SESSION_COOKIE_SECURE"] = patient_auth.COOKIE_SECURE
    # this app takes no uploads - the cap is a bound on form bodies, chosen
    # not locked (WR-06). no 413 handler: app/__init__.py's exists because
    # its upload form needs a flashed banner, and a patient hitting a 64KB
    # cap on a two-field login form is not a flow worth designing for
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
    # the more exposed surface does not get weaker defaults than the staff app
    CSRFProtect(app)

    # read routes.DB_PATH at call time, not by importing the value - the
    # selftest patches this attribute, and copying it at import would freeze
    # the wrong path (mirrors app/__init__.py's db.DB_PATH comment). a first
    # boot before the staff app has ever run is a plausible ordering: without
    # this, db/ doesn't exist and every request 500s, or db/ exists without
    # clinic.sqlite and sqlite creates an empty file that shadows the real
    # one (WR-02). init_db already calls patient_auth.init_patient_tables.
    Path(routes.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = storage.init_db(routes.DB_PATH)
    conn.close()

    @app.teardown_appcontext
    def _close_db(exception):
        # app.run(threaded=True) means several requests can be live at once,
        # each holding a WAL reader - closure must not depend on refcounting
        # eventually collecting g (WR-03). inline rather than a patient_app/
        # db.py: this module has no separate db module, and manufacturing
        # one for a single function is not proportionate.
        db = g.pop("db", None)
        if db is not None:
            db.close()

    app.register_blueprint(patient_bp)
    app.before_request(require_patient_session)

    @app.context_processor
    def inject_language():
        # g.patient is None on the login screen - no session exists there,
        # and the same context processor runs for every route. the template
        # uses this only as a presence check to decide whether to render the
        # logout control; it never reads a field off it.
        return {"t": t, "lang": current_language(), "patient": g.get("patient")}

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
        # gated by require_patient_session above - reachable only with a
        # session past the forced pin change. the real content is Phase 18's
        # chat surface; this is still a placeholder landing page.
        return render_template("patient_home.html")

    return app
