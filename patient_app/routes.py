"""patient-facing routes: login, logout, and the forced pin change.

the session gate (no session -> /login, must_change_pin -> /change-pin) is one
before_request function so the "don't trap the user" property lives in one
place, the same shape app/__init__.py uses for CHANGE_PW_ALLOWED.
"""

from pathlib import Path

from flask import Blueprint, current_app, g, redirect, render_template, request, url_for

import patient_auth
import storage
from auth import log_audit

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
NO_SESSION_ALLOWED = {"static", "vendor", "patient.login", "set_language"}

# reachable while must_change_pin is still set - mirrors CHANGE_PW_ALLOWED's
# shape so the "don't trap the user" property is visible in one place
FORCED_CHANGE_ALLOWED = {
    "static", "vendor", "patient.change_pin", "set_language", "patient.logout",
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
    # remote_addr is recorded as evidence for the throttle and the audit row, not
    # trusted as identity - see the residual note in patient_auth.verify_pin
    status, _row = patient_auth.verify_pin(cf, pin, conn, ip=request.remote_addr)

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
    log_audit(conn, cf, "patient", "patient_login", cf, allowed=1, ip=request.remote_addr)

    resp = redirect(url_for("home"))
    resp.set_cookie(
        patient_auth.PATIENT_COOKIE_NAME, token,
        httponly=True, samesite="Strict", secure=current_app.config["SESSION_COOKIE_SECURE"],
    )
    return resp


@patient_bp.route("/logout", methods=["POST"])
def logout():
    token = request.cookies.get(patient_auth.PATIENT_COOKIE_NAME)
    conn = get_db()
    if token:
        session = patient_auth.load_patient_session(conn, token)
        if session:
            log_audit(conn, session["codice_fiscale"], "patient", "patient_logout",
                       session["codice_fiscale"], allowed=1, ip=request.remote_addr)
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
        patient_auth.change_pin(cf, pin, conn, current_pin=current)
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
