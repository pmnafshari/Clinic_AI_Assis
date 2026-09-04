"""The public clinic site. Unauthenticated by design.

The staff and patient apps default-deny every route through a before_request
gate. This one has no gate, because it has nothing to gate: no database
connection, no session, no patient module, no secret key. The safety comes
from the app having nothing to leak, not from a check a later edit could
lose - and site_app_selftest asserts that structurally.

Phase 32 gives it exactly one page: the design-system reference. Phase 33
fills it with the real clinic site.
"""

from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory

from shared import STATIC_ROOT as SHARED_STATIC_ROOT

from . import clinic_answers, content, voice_routes


def create_site_app():
    app = Flask(__name__)

    # No SECRET_KEY. This app writes no session and flashes nothing, so it
    # sets no cookie at all - which is a stronger position than owning a
    # third key file. If a later phase needs session state, Flask will raise
    # rather than silently sign with a default, forcing that to be a decision
    # someone makes rather than one inherited from this skeleton.
    #
    # The name is still set: all three apps share a host and cookies are not
    # port-scoped (CR-05), so if session state ever does arrive here it must
    # not land on "session" and clobber a staff or patient csrf cookie.
    app.config["SESSION_COOKIE_NAME"] = "site_session"
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Strict"

    # read once at construction, not per request - it is a file that changes
    # when someone edits it, and a reload is the honest way to pick that up
    clinic = content.load()

    # image slots are declared in clinic.yaml whether or not a file exists
    # yet. this decides, server-side, whether a slot renders a real <img> or
    # the placeholder that holds its proportion - so a missing photograph is
    # an honest gap rather than a broken image icon, with no javascript.
    img_root = Path(app.static_folder)

    @app.template_global()
    def has_image(rel_path):
        if not rel_path:
            return False
        candidate = (img_root / rel_path).resolve()
        # never let a config value walk out of static/
        if not str(candidate).startswith(str(img_root.resolve())):
            return False
        return candidate.is_file()

    @app.context_processor
    def inject_clinic():
        # every template gets it, so no route has to remember to pass it and
        # no template has to reach for a literal instead
        return {"clinic": clinic}

    @app.route("/shared/<path:filename>")
    def shared(filename):
        # the same directory the other two apps read - one token set, three
        # consumers, no copy that can drift
        return send_from_directory(SHARED_STATIC_ROOT, filename)

    @app.route("/")
    def home():
        # trivial on purpose - the context processor already supplies clinic
        return render_template("home.html")

    # secondary pages. each reuses the landing page's section partials
    # rather than restating them, so content stays single-sourced.
    @app.route("/services")
    def services():
        return render_template("services.html")

    @app.route("/doctors")
    def doctors():
        return render_template("doctors.html")

    @app.route("/clinic")
    def clinic_page():
        return render_template("clinic.html")

    @app.route("/assistant", methods=["GET", "POST"])
    def assistant():
        # public, and holds no data: it answers clinic questions out of
        # clinic.yaml and hands anything personal to sign-in, where the
        # scoped chat lives. no database is reachable from this app at all.
        if request.method == "GET":
            return render_template("assistant.html")
        asked = request.form.get("question", "").strip()[:500]
        state, text = clinic_answers.answer(asked, clinic)
        return render_template("assistant.html", state=state, reply=text, asked=asked)

    @app.route("/assistant/ask", methods=["POST"])
    def assistant_ask():
        # json turn endpoint, so the conversation continues without a reload.
        # the page still works with javascript off - /assistant handles a
        # plain form post and renders the same answer - this only upgrades it.
        asked = (request.form.get("question") or "").strip()[:500]
        state, text = clinic_answers.answer(asked, clinic)
        return jsonify({"state": state, "text": text, "asked": asked})

    voice_routes.register(app, clinic)

    @app.route("/contact")
    def contact():
        return render_template("contact.html")

    @app.route("/reference")
    def reference():
        return render_template("reference.html")

    return app
