"""patient-facing routes: login, logout, and the forced pin change.

the session gate (no session -> /login, must_change_pin -> /change-pin) is one
before_request function so the "don't trap the user" property lives in one
place, the same shape app/__init__.py uses for CHANGE_PW_ALLOWED.
"""

from pathlib import Path

from flask import Blueprint, current_app, g, redirect, render_template, request, url_for

import appointments
import patient_accessor
import patient_auth
import storage
from auth import log_audit

from . import chat, net
from .strings import current_language, t

patient_bp = Blueprint("patient", __name__)

# anchors every cwd-relative path in this app on the repo root instead of the
# process's launch directory. this file lives at patient_app/routes.py, so
# two parents reach the repo root. launched from anywhere else, the app used
# to serve an unstyled login page, write a stray .env.patient (generating a
# fresh SECRET_KEY and invalidating every live CSRF session), and open or
# create the wrong database (WR-11).
REPO_ROOT = Path(__file__).resolve().parent.parent

# selftests point this at a temp db before calling create_patient_app, same
# pattern as app/db.py's DB_PATH. stays a plain string, not REPO_ROOT itself,
# so a selftest can still reassign it.
DB_PATH = str(REPO_ROOT / "db" / "clinic.sqlite")

# reachable with no patient session at all
NO_SESSION_ALLOWED = {"static", "vendor", "shared", "patient.login", "set_language"}

# reachable while must_change_pin is still set - mirrors CHANGE_PW_ALLOWED's
# shape so the "don't trap the user" property is visible in one place
FORCED_CHANGE_ALLOWED = {
    "static", "vendor", "shared", "patient.change_pin", "set_language",
    "patient.logout",
}

STATUS_TO_ERROR_KEY = {
    # wrong and unknown share a string on purpose - this surface is
    # internet-reachable and must not confirm whether a codice fiscale
    # belongs to a patient of this clinic
    "wrong": "err_bad_credentials",
    "unknown": "err_bad_credentials",
    "expired": "err_expired",
    "locked": "err_locked",
    # a throttled source learns nothing it did not already know, so the body
    # must stay byte-identical to the other refusals
    "throttled": "err_bad_credentials",
}


def get_db():
    # WR-12, residual stated where the connection is actually opened. the
    # separation the selftests assert (patient_app_selftest.py section 1,
    # section 23f) is a module-import separation - patient_auth never
    # imports web_auth/web_session, and this file's own SQL never names a
    # staff table. at runtime this is still an ordinary read-write sqlite
    # connection to DB_PATH, which also holds users (password hashes),
    # sessions (staff session hashes), audit_log and every clinical note.
    # SQLite has no per-table privileges, so any SQL-reachable defect in this
    # internet-facing app is a full compromise of the staff credential
    # store, not a patient-scoped one.
    #
    # what narrows it: every patient-side query stays parameterised and
    # confined to patient_*, patients and visits, and section 23f fails the
    # suite if any patient-side module ever names a staff table in a SQL
    # clause. Phase 17's D-05 (direct connection, accessor-only) is not
    # re-opened here - this states and narrows the residual, it does not
    # replace it. a genuinely separate database file, or an attached
    # read-only view of the patient-visible tables, is the real fix and
    # belongs with Phase 20's tunnel work.
    if "db" not in g:
        g.db = storage.connect(DB_PATH)
    return g.db


def require_patient_session():
    if request.endpoint is None:
        return  # unmatched route - let the normal 404 flow run
    if request.endpoint in NO_SESSION_ALLOWED:
        return

    token = request.cookies.get(patient_auth.PATIENT_COOKIE_NAME)
    session = patient_auth.load_patient_session(get_db(), token) if token else None
    if session is None:
        return redirect(url_for("patient.login"))
    g.patient = session

    if session["must_change_pin"] and request.endpoint not in FORCED_CHANGE_ALLOWED:
        return redirect(url_for("patient.change_pin"))


@patient_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("patient_login.html")

    cf = request.form.get("codice_fiscale", "").strip().upper()
    pin = request.form.get("pin", "")

    # unbounded form input goes straight into werkzeug's key derivation
    # function - a handful of concurrent multi-megabyte posts would pin cpu
    # and memory on a machine that also has to hold a language model. the
    # generic message is deliberate - this refusal must read like every
    # other one (WR-06)
    if len(pin) > 128:
        error = t("err_bad_credentials", current_language())
        return render_template("patient_login.html", error=error)

    conn = get_db()
    # evidence for the throttle and the audit row, never identity - see the
    # residual note in patient_auth.verify_pin. resolved through net's trust
    # boundary now, so behind the tunnel this is the patient's address rather
    # than cloudflared's; without the flag it is still just the socket peer.
    status, _row = patient_auth.verify_pin(cf, pin, conn, ip=net.from_request(request))

    if status != "ok":
        # fail closed on the generic refusal - it's also the safe enumeration
        # answer, so the defensive default and the security default are the
        # same string. the .get is the backstop for the next status nobody
        # remembers to map (WR-13)
        error = t(STATUS_TO_ERROR_KEY.get(status, "err_bad_credentials"), current_language())
        return render_template("patient_login.html", error=error)

    token = patient_auth.create_patient_session(conn, cf)
    # a name that is not "login" so patient and staff sign-ins are
    # distinguishable in one audit trail (phase 16-01 precedent)
    log_audit(conn, cf, "patient", "patient_login", cf, allowed=1, ip=net.from_request(request))

    resp = redirect(url_for("home"))
    resp.set_cookie(
        patient_auth.PATIENT_COOKIE_NAME, token,
        httponly=True, samesite="Strict", secure=current_app.config["SESSION_COOKIE_SECURE"],
    )
    return resp


@patient_bp.route("/profile")
def profile():
    # demographics come through patient_accessor, the same scope-checked path
    # the chat uses - not a second query. a mismatched cf writes a
    # patient_scope_violation row there and returns nothing, exactly as it
    # does for the chat. no visit or invoice is read here.
    cf = g.patient["codice_fiscale"]
    demo = patient_accessor.get_demographics(cf, get_db(), ip=net.from_request(request))
    return render_template("patient_profile.html", cf=cf, demo=demo)


@patient_bp.route("/logout", methods=["POST"])
def logout():
    token = request.cookies.get(patient_auth.PATIENT_COOKIE_NAME)
    conn = get_db()
    if token:
        session = patient_auth.load_patient_session(conn, token)
        if session:
            log_audit(conn, session["codice_fiscale"], "patient", "patient_logout",
                       session["codice_fiscale"], allowed=1, ip=net.from_request(request))
        patient_auth.destroy_patient_session(conn, token)

    resp = redirect(url_for("patient.login"))
    resp.delete_cookie(patient_auth.PATIENT_COOKIE_NAME)
    return resp


@patient_bp.route("/change-pin", methods=["GET", "POST"])
def change_pin():
    if request.method == "GET":
        return render_template("patient_change_pin.html")

    current = request.form.get("current", "")
    pin = request.form.get("pin", "")
    confirm = request.form.get("confirm", "")
    lang = current_language()

    if pin != confirm:
        return render_template(
            "patient_change_pin.html", error=t("err_pin_mismatch", lang)
        )

    conn = get_db()
    cf = g.patient["codice_fiscale"]
    try:
        patient_auth.change_pin(cf, pin, conn, current_pin=current, ip=net.from_request(request))
    except ValueError as exc:
        reason = str(exc)
        if reason == "short":
            error = t("err_pin_short", lang, n=patient_auth.PIN_LENGTH)
        elif reason == "weak":
            error = t("err_pin_weak", lang)
        elif reason == "same":
            error = t("err_pin_same", lang)
        elif reason == "current":
            # the screen is behind a session, so the copy must not read as
            # confirmation that some other field was accepted
            error = t("err_bad_credentials", lang)
        else:
            error = t("err_bad_credentials", lang)
        return render_template("patient_change_pin.html", error=error)

    # change_pin just destroyed every session for this codice fiscale,
    # including this browser's - hand it a fresh token rather than bouncing
    # it to the login screen for changing its own pin. a stolen token is
    # dead even in the case where the stolen token is the one being used (D-09)
    token = patient_auth.create_patient_session(conn, cf)
    resp = redirect(url_for("home"))
    resp.set_cookie(
        patient_auth.PATIENT_COOKIE_NAME, token,
        httponly=True, samesite="Strict", secure=current_app.config["SESSION_COOKIE_SECURE"],
    )
    return resp


# neither NO_SESSION_ALLOWED nor FORCED_CHANGE_ALLOWED lists this endpoint,
# so require_patient_session gates it by default - the whole point of that
# whitelist being a whitelist (§5.4's default-deny guard)
@patient_bp.route("/chat", methods=["GET", "POST"], endpoint="chat")
def chat_page():
    if request.method == "GET":
        return render_template("patient_chat.html")

    # exactly one field is named "question" - the text input. the example
    # chips fill it from javascript rather than submitting their own value,
    # so there is no second value here to pick between.
    question = request.form.get("question", "").strip()

    # unbounded form text would reach the keyword gate and then a language
    # model on a machine that also has to hold that model - same reasoning
    # as the 128-character pin cap above (WR-06). 500 is chosen here, not
    # locked. truncating rather than refusing avoids inventing a fifth
    # response state beyond the four the design contract locks.
    question = question[:500]

    # the session row is the only source of cf for this call, never the
    # submitted form (§3.2, D-08)
    cf = g.patient["codice_fiscale"]

    conn = get_db()
    result = chat.answer_question(question, cf, conn, current_language(), ip=net.from_request(request))

    # no audit call here on purpose. CHAT-07's per-interaction row is written
    # inside chat.answer_question's wrapper, on this same code path, and the
    # scope-mismatch row inside patient_accessor - a call here would just
    # double-count what those already record.
    # the question is passed back so the page can render the exchange as an
    # exchange rather than a lone answer. presentational only - it is the
    # value already read above, jinja-escaped on the way out, and capped at
    # 500 characters by the truncation a few lines up.
    return render_template(
        "patient_chat.html", state=result["state"], body=result["body"], question=question
    )


# --- appointments (Phase 42) ----------------------------------------------
#
# A patient requests a day and a half of it; the clinic confirms the slot.
# They never write into a dentist's calendar - see appointments.py for why the
# schema cannot support a slot picker honestly.
#
# Every action here goes through appointments.owned_by(). Reaching for another
# patient's appointment id is not a 404 to be shrugged off: it is logged as a
# patient_scope_violation, exactly as patient_accessor does for a mismatched
# codice fiscale, because the two are the same attack seen from different
# tables.

def _deny(conn, cf, action):
    log_audit(conn, cf, "patient", "patient_scope_violation", action,
              allowed=0, ip=net.from_request(request))


@patient_bp.route("/appointments")
def appointments_page():
    cf = g.patient["codice_fiscale"]
    booked, requested = appointments.open_for_patient(get_db(), cf)
    return render_template("patient_appointments.html",
                           booked=booked, requested=requested)


@patient_bp.route("/appointments/request", methods=["POST"])
def appointments_request():
    cf = g.patient["codice_fiscale"]
    conn = get_db()
    lang = current_language()
    day = (request.form.get("day") or "").strip()
    period = (request.form.get("period") or "").strip()
    reason = (request.form.get("reason") or "").strip()[:200] or None
    try:
        new_id = appointments.request(conn, cf, day, period, reason)
    except ValueError as e:
        # the module's message names the rule; the page says it in the
        # patient's language rather than echoing english from a domain module
        key = "appt_error_date" if "date" in str(e) else (
            "appt_error_period" if "morning" in str(e) else "appt_error_generic")
        booked, requested = appointments.open_for_patient(conn, cf)
        return render_template("patient_appointments.html", booked=booked,
                               requested=requested, error=t(key, lang)), 400
    log_audit(conn, cf, "patient", "patient_appointment_request", str(new_id),
              allowed=1, ip=net.from_request(request))
    return redirect(url_for("patient.appointments_page", done="requested"))


@patient_bp.route("/appointments/<int:appointment_id>/cancel", methods=["POST"])
def appointments_cancel(appointment_id):
    cf = g.patient["codice_fiscale"]
    conn = get_db()
    row = appointments.owned_by(conn, appointment_id, cf)
    if row is None:
        # someone else's id, or none at all. same response either way - a
        # different one would confirm the appointment exists.
        _deny(conn, cf, "patient_appointment_cancel")
        return redirect(url_for("patient.appointments_page"))
    if row["status"] != appointments.BOOKED:
        # a request is withdrawn, not cancelled, and a cancelled one is
        # already done. neither is a scope violation.
        return redirect(url_for("patient.appointments_page"))
    appointments.cancel(conn, appointment_id)
    log_audit(conn, cf, "patient", "patient_appointment_cancel", str(appointment_id),
              allowed=1, ip=net.from_request(request))
    return redirect(url_for("patient.appointments_page", done="cancelled"))
