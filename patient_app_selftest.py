import json
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from flask import url_for
from markupsafe import escape as html_escape

import patient_accessor
import patient_app
import patient_auth
import storage
import web_session
from patient_app import create_patient_app
from patient_app import routes as patient_routes
from patient_app.strings import DEFAULT_LANGUAGE, LANG_COOKIE_NAME, LANGUAGES, STRINGS, t

STAFF_ENDPOINTS = ("patients.detail_view", "auth.login", "qa.qa_page", "admin.users_view")


def _csrf_from(html):
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return match.group(1)


def _strip_csrf(html):
    return re.sub(r'name="csrf_token" value="[^"]+"', "", html)


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "clinic.sqlite")
        conn = storage.init_db(db_path)
        patient_auth.init_patient_tables(conn)
        conn.close()

        # this file must not depend on the staff app - point routes.py at
        # our own temp db, same pattern as app/db.py's DB_PATH
        patient_routes.DB_PATH = db_path

        app = create_patient_app(env_path=Path(tmp) / ".env.patient")
        app.config["TESTING"] = True

        # 1. isolation, asserted rather than assumed
        endpoints = set(app.view_functions)
        for name in STAFF_ENDPOINTS:
            assert name not in endpoints, f"1: staff endpoint {name} must not exist on the patient app"
        assert patient_auth.PATIENT_COOKIE_NAME != web_session.COOKIE_NAME, \
            "1: the two apps must not share a cookie name"
        # flask's own session cookie (flask_wtf's csrf token storage) must not
        # alias the patient auth cookie - aliasing them makes the two Set-
        # Cookie writers silently overwrite each other (see patient_app/__init__.py)
        assert app.config.get("SESSION_COOKIE_NAME", "session") != patient_auth.PATIENT_COOKIE_NAME, \
            "1: flask's session cookie must not collide with the patient auth cookie"
        # the collision that actually happens: both apps share a host, and
        # RFC 6265 cookies are not port-scoped, so leaving either one at
        # Flask's default "session" lets them clobber each other's csrf
        # session, each signed with a different SECRET_KEY (CR-05)
        assert app.config["SESSION_COOKIE_NAME"] != "session", \
            "1: the two apps share a host, and cookies are not port-scoped"
        assert app.config["SESSION_COOKIE_NAME"] != web_session.COOKIE_NAME, \
            "1: must not collide with the staff app's own auth cookie name either"

        import ast
        import inspect
        tree = ast.parse(inspect.getsource(sys.modules["patient_app"]))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for forbidden in ("web_auth", "web_session", "app"):
            assert forbidden not in imported, \
                f"1: the patient app must not import {forbidden} - binding separation property"

        client = app.test_client()

        # 2. no CDN - offline-first, and this is the internet-facing surface.
        # checked against /login: the app root now requires a session (SC3)
        login_page = client.get("/login")
        assert login_page.status_code == 200, "2: the login page should render"
        assert not re.search(
            r'<(?:script|link)[^>]+(?:src|href)="https?://', login_page.text, re.IGNORECASE
        ), "2: the patient surface must not reference any external asset"
        assert client.get("/vendor/bootstrap/5.3.3/bootstrap.min.css").status_code == 200, \
            "2: the vendored bootstrap should serve locally"

        # 3. italian is the default, not a fallback
        assert DEFAULT_LANGUAGE == "it"
        assert t("login_heading", "it") in login_page.text, "3: default page should be italian"
        assert t("login_heading", "en") not in login_page.text, "3: english must not leak into the default"

        # 4. the switch persists across requests - this is what proves the
        # cookie is carrying the choice. a page-scoped implementation passes
        # the first half of this and fails the second.
        switched = client.get("/lang/en", follow_redirects=True)
        assert t("login_heading", "en") in switched.text, "4: switching should render english"
        again = client.get("/login")
        assert t("login_heading", "en") in again.text, \
            "4: the language choice must survive to the next page"
        client.get("/lang/it", follow_redirects=True)
        assert t("login_heading", "it") in client.get("/login").text, "4: and switch back"

        # 5. an off-list value is ignored rather than stored
        bad = client.get("/lang/zz", follow_redirects=True)
        assert bad.status_code == 200, "5: a bad language must not 500"
        assert t("login_heading", DEFAULT_LANGUAGE) in bad.text, \
            "5: an off-list language should fall back to the default"

        # 6. no open redirect - the target is built in-app, never from Referer
        hostile = client.get("/lang/en", headers={"Referer": "https://evil.example/"})
        assert "evil.example" not in hostile.headers.get("Location", ""), \
            "6: the language redirect must not follow the Referer header"

        # 7. every string exists in both languages, and the two differ
        for key in STRINGS:
            for lang in LANGUAGES:
                value = t(key, lang)
                assert value and value.strip(), f"7: {key}/{lang} is empty"
        assert t("login_heading", "it") != t("login_heading", "en"), \
            "7: the two languages must actually differ"
        assert t("login_heading", "zz") == t("login_heading", DEFAULT_LANGUAGE), \
            "7: an unknown language should fall back"
        try:
            t("no_such_key", "it")
            raise AssertionError("7: an unknown key must raise")
        except KeyError:
            pass

        # 8. SC4's refusal copy names the clinic, in both languages
        assert "clinica" in t("err_expired", "it").lower()
        assert "clinic" in t("err_expired", "en").lower()
        assert "clinica" in t("err_locked", "it").lower()
        assert "clinic" in t("err_locked", "en").lower()
        assert "{n}" not in t("err_pin_short", "it", n=8)
        assert "8" in t("err_pin_short", "it", n=8)

        # 9. the language cookie is not the session cookie
        assert LANG_COOKIE_NAME != patient_auth.PATIENT_COOKIE_NAME, \
            "9: the language preference must not ride on the session cookie"

        # ---- route-level coverage of the phase's four success criteria ----

        def raw_db():
            c = sqlite3.connect(db_path)
            c.row_factory = sqlite3.Row
            return c

        def count(sql, args=()):
            c = raw_db()
            n = c.execute(sql, args).fetchone()[0]
            c.close()
            return n

        def seed_and_issue(cf, name="test patient"):
            c = raw_db()
            c.execute(
                "INSERT OR IGNORE INTO patients (codice_fiscale, patient_name) VALUES (?, ?)",
                (cf, name),
            )
            c.commit()
            pin = patient_auth.issue_pin(cf, c, "test-dentist", "dentist")
            c.close()
            return pin

        def sign_in(cf, pin):
            c = app.test_client()
            page = c.get("/login")
            resp = c.post("/login", data={
                "codice_fiscale": cf, "pin": pin, "csrf_token": _csrf_from(page.text),
            })
            return c, resp

        # 10. SC2 - login works
        cf_ok = "FRRR850010150200"
        pin_ok = seed_and_issue(cf_ok)
        client_ok, resp_ok = sign_in(cf_ok, pin_ok)
        assert resp_ok.status_code == 302, "10: a correct login should redirect"
        assert count(
            "SELECT COUNT(*) FROM patient_sessions WHERE codice_fiscale = ?", (cf_ok,)
        ) == 1, "10: a session row should exist for this codice fiscale"

        # 11. SC2 - nothing touches the staff tables. the criterion's literal
        # wording names both users and sessions, not just the cookie.
        assert count("SELECT COUNT(*) FROM users WHERE username = ?", (cf_ok,)) == 0, \
            "11: the staff users table must stay untouched"
        assert count("SELECT COUNT(*) FROM sessions WHERE username = ?", (cf_ok,)) == 0, \
            "11: the staff sessions table must stay untouched"
        set_cookie = resp_ok.headers.get("Set-Cookie", "")
        assert patient_auth.PATIENT_COOKIE_NAME in set_cookie, "11: expected the patient cookie to be set"
        assert web_session.COOKIE_NAME not in set_cookie, "11: must not set the staff cookie"

        # 12. SC3 - forced change gates everything except the change screen,
        # the language toggle and logout
        cf_forced = "RSSM800010150100"
        pin_forced = seed_and_issue(cf_forced)
        client_forced, _ = sign_in(cf_forced, pin_forced)

        root = client_forced.get("/")
        assert root.status_code == 302 and "change-pin" in root.headers["Location"], \
            "12: the app root should redirect to change-pin while must_change_pin is set"
        assert client_forced.get("/lang/en", follow_redirects=True).status_code == 200, \
            "12: the language toggle must still work while forced"
        client_forced.get("/lang/it", follow_redirects=True)
        change_screen = client_forced.get("/change-pin")
        logout_resp = client_forced.post(
            "/logout", data={"csrf_token": _csrf_from(change_screen.text)}
        )
        assert logout_resp.status_code == 302, "12: logout must still work while forced (not trapped)"

        client_forced, _ = sign_in(cf_forced, pin_forced)
        change_page = client_forced.get("/change-pin")
        assert change_page.status_code == 200, "12: the change-pin screen should be reachable"
        done = client_forced.post("/change-pin", data={
            "pin": "13579246", "confirm": "13579246",
            "csrf_token": _csrf_from(change_page.text),
        })
        assert done.status_code == 302, "12: a completed change should redirect"
        assert client_forced.get("/").status_code == 200, \
            "12: the root should be reachable once the change is complete"
        assert count(
            "SELECT must_change_pin FROM patient_credentials WHERE codice_fiscale = ?", (cf_forced,)
        ) == 0, "12: must_change_pin should be cleared"

        # D-16 reconciliation (17.1-03). CHAT-03's original criterion said,
        # and still says: a patient who holds a real credential and cannot
        # get in is told to contact the clinic. what changed under D-01's
        # reorder is *when* that telling happens - the expired and locked
        # messages below are now reachable only by a caller who has already
        # submitted the correct pin, because revealing them to anyone who
        # merely guesses a codice fiscale was the enrolment oracle CR-03
        # described. what still gets the genuinely-forgot-their-pin patient
        # to the clinic is D-02's help_line: a permanent, unconditional
        # "trouble signing in? contact the clinic" line rendered on every
        # response, correct pin or not, so it carries no information and
        # needs no oracle to reach them. the rule for whoever reads this
        # next: help_line must stay unconditional (section 18 already pins
        # that structurally) - making it conditional would re-open the
        # oracle this reorder closed. sections 13 and 14 below pass with
        # their original assertions unmodified; only the throttle reset and
        # the wrong-pin negative case were added.

        # reset the throttle before this section - every test client here
        # reports 127.0.0.1, so without this reset sections 13-15 would
        # throttle each other and it would read as an application defect
        # rather than the plumbing this file's test clients share
        reset13 = raw_db()
        reset13.execute("DELETE FROM patient_login_attempts")
        reset13.commit()
        reset13.close()

        # 13. SC4 - an expired credential is refused with the "contact the
        # clinic" copy, in both languages, never the generic message
        cf_expired = "BNCG900010150300"
        pin_expired = seed_and_issue(cf_expired)
        c = raw_db()
        c.execute(
            "UPDATE patient_credentials SET expires_at = ? WHERE codice_fiscale = ?",
            ((datetime.now() - timedelta(days=1)).isoformat(), cf_expired),
        )
        c.commit()
        c.close()

        expired_client = app.test_client()
        page = expired_client.get("/login")
        resp = expired_client.post("/login", data={
            "codice_fiscale": cf_expired, "pin": pin_expired, "csrf_token": _csrf_from(page.text),
        })
        assert resp.status_code == 200, "13: an expired credential should re-render, not redirect"
        assert t("err_expired", "it") in resp.text, "13: expected the expired copy in italian"
        assert t("err_bad_credentials", "it") not in resp.text, \
            "13: expired must not collapse into the generic refusal"

        en_client = app.test_client()
        en_client.get("/lang/en", follow_redirects=True)
        page_en = en_client.get("/login")
        resp_en = en_client.post("/login", data={
            "codice_fiscale": cf_expired, "pin": pin_expired, "csrf_token": _csrf_from(page_en.text),
        })
        assert t("err_expired", "en") in resp_en.text, "13: expected the expired copy in english"

        # D-01's negative case: the same expired credential probed with the
        # wrong pin must render the generic refusal, not the expired copy -
        # expired is reachable only by a caller who already knows the pin
        wrong_expired_client = app.test_client()
        wrong_expired_page = wrong_expired_client.get("/login")
        wrong_expired_resp = wrong_expired_client.post("/login", data={
            "codice_fiscale": cf_expired, "pin": "00000000",
            "csrf_token": _csrf_from(wrong_expired_page.text),
        })
        assert t("err_bad_credentials", "it") in wrong_expired_resp.text, \
            "13: an expired credential probed with the wrong pin must render the generic refusal"
        assert t("err_expired", "it") not in wrong_expired_resp.text, \
            "13: a wrong pin must not reveal that the credential is expired"

        # reset the throttle before this section - see the comment above
        # section 13
        reset14 = raw_db()
        reset14.execute("DELETE FROM patient_login_attempts")
        reset14.commit()
        reset14.close()

        # 14. SC4 - a locked-out credential gives no distinct signal even for
        # the correct pin, in both languages
        cf_locked = "VRDL910010150400"
        pin_locked = seed_and_issue(cf_locked)
        lock_client = app.test_client()
        for _ in range(patient_auth.PIN_LOCKOUT_THRESHOLD):
            page = lock_client.get("/login")
            lock_client.post("/login", data={
                "codice_fiscale": cf_locked, "pin": "00000000", "csrf_token": _csrf_from(page.text),
            })
        page = lock_client.get("/login")
        locked_resp = lock_client.post("/login", data={
            "codice_fiscale": cf_locked, "pin": pin_locked, "csrf_token": _csrf_from(page.text),
        })
        assert t("err_locked", "it") in locked_resp.text, \
            "14: the correct pin during lockout must still show the locked copy"

        en_lock_client = app.test_client()
        en_lock_client.get("/lang/en", follow_redirects=True)
        page = en_lock_client.get("/login")
        en_lock_client.post("/login", data={
            "codice_fiscale": cf_locked, "pin": pin_locked, "csrf_token": _csrf_from(page.text),
        })
        page = en_lock_client.get("/login")
        locked_resp_en = en_lock_client.post("/login", data={
            "codice_fiscale": cf_locked, "pin": pin_locked, "csrf_token": _csrf_from(page.text),
        })
        assert t("err_locked", "en") in locked_resp_en.text, \
            "14: expected the locked copy in english too"

        # D-01's negative case: the same locked credential probed with the
        # wrong pin must render the generic refusal, not the locked copy -
        # locked is reachable only by a caller who already knows the pin
        wrong_locked_client = app.test_client()
        wrong_locked_page = wrong_locked_client.get("/login")
        wrong_locked_resp = wrong_locked_client.post("/login", data={
            "codice_fiscale": cf_locked, "pin": "00000000",
            "csrf_token": _csrf_from(wrong_locked_page.text),
        })
        assert t("err_bad_credentials", "it") in wrong_locked_resp.text, \
            "14: a locked credential probed with the wrong pin must render the generic refusal"
        assert t("err_locked", "it") not in wrong_locked_resp.text, \
            "14: a wrong pin must not reveal that the account is locked"

        # reset the throttle before this section - see the comment above
        # section 13
        reset15 = raw_db()
        reset15.execute("DELETE FROM patient_login_attempts")
        reset15.commit()
        reset15.close()

        # 15. enumeration - a wrong pin and an unknown codice fiscale must be
        # indistinguishable. byte-identical bodies (apart from the csrf
        # token) is the strongest available form of that guarantee.
        unknown_cf = "ZZZZ999999999999"
        unknown_client = app.test_client()
        page = unknown_client.get("/login")
        unknown_resp = unknown_client.post("/login", data={
            "codice_fiscale": unknown_cf, "pin": "00000000", "csrf_token": _csrf_from(page.text),
        })

        cf_wrong = "PLLM920010150500"
        seed_and_issue(cf_wrong)
        wrong_client = app.test_client()
        page2 = wrong_client.get("/login")
        wrong_resp = wrong_client.post("/login", data={
            "codice_fiscale": cf_wrong, "pin": "00000000", "csrf_token": _csrf_from(page2.text),
        })

        assert unknown_resp.status_code == 200 and wrong_resp.status_code == 200, \
            "15: both refusals should re-render the login form"
        assert _strip_csrf(unknown_resp.text) == _strip_csrf(wrong_resp.text), \
            "15: an unknown codice fiscale and a wrong pin must render byte-identical bodies"

        # 16. DEF-1 - the logout control must be reachable from rendered
        # HTML, not just provable by driving the route directly. section 12
        # above proved the mechanism works; it never looked at the page.
        # that gap is exactly how the live walk found this defect.
        with app.test_request_context():
            logout_url = url_for("patient.logout")
        logout_form_re = re.compile(
            r'<form[^>]*action="' + re.escape(logout_url) + r'"[^>]*>.*?</form>', re.DOTALL
        )

        cf_logout = "MRTN930010150600"
        pin_logout = seed_and_issue(cf_logout)
        logout_client, _ = sign_in(cf_logout, pin_logout)
        change_page = logout_client.get("/change-pin")
        logout_client.post("/change-pin", data={
            "pin": "24681357", "confirm": "24681357",
            "csrf_token": _csrf_from(change_page.text),
        })
        home_page = logout_client.get("/")
        assert home_page.status_code == 200, "16: the home page should render"
        home_form = logout_form_re.search(home_page.text)
        assert home_form, "16: expected a form posting to patient.logout on the home screen"
        assert t("logout_cta", "it") in home_form.group(0), \
            "16: the logout control must carry the logout_cta string"
        assert 'name="csrf_token"' in home_form.group(0), \
            "16: the logout form must carry a csrf token - the route is POST-only and CSRF-protected"

        # same control, still on the forced change-pin screen - the screen it
        # matters most on, since it's the only screen a first-login patient
        # can reach, and FORCED_CHANGE_ALLOWED already keeps the route open
        cf_forced_logout = "GRLL940010150700"
        pin_forced_logout = seed_and_issue(cf_forced_logout)
        forced_logout_client, _ = sign_in(cf_forced_logout, pin_forced_logout)
        change_screen = forced_logout_client.get("/change-pin")
        change_form = logout_form_re.search(change_screen.text)
        assert change_form, "16: the forced change-pin screen must render the logout control too"
        assert t("logout_cta", "it") in change_form.group(0), \
            "16: the change-pin screen's logout button must carry the logout_cta string"

        # absent where there is no session to end - rendering it there would
        # be a dead control
        anon_login_page = app.test_client().get("/login")
        assert not logout_form_re.search(anon_login_page.text), \
            "16: the login screen has no session, so it must not render a logout control"

        # 17. DEF-2 - the card keeps the UI-SPEC's xl (32px = 2rem) minimum
        # margin from the viewport edge on both axes. this is a source-level
        # assertion, not a computed-style check - this project has no
        # rendering harness, and adding one for a one-line margin is not
        # proportionate. it pins the value so a later edit can't silently
        # drift the horizontal margin back down to the sm token.
        css_text = Path("patient_app/static/css/patient.css").read_text()
        shell_match = re.search(r"\.patient-shell\s*\{([^}]*)\}", css_text, re.DOTALL)
        assert shell_match, "17: expected a .patient-shell rule in patient.css"
        padding_match = re.search(r"padding:\s*([^;]+);", shell_match.group(1))
        assert padding_match, "17: expected a padding declaration on .patient-shell"
        assert padding_match.group(1).strip() == "2rem", \
            "17: .patient-shell padding must be 2rem on both axes (the spec's xl/32px token)"

        # 18. D-02/WR-05/WR-09/WR-13 - written first, observed failing (17.1-02).
        # D-02: the clinic line is unconditional - present on an anonymous GET,
        # on a wrong-pin refusal and on an unknown-cf refusal, in both languages.
        anon_client = app.test_client()
        anon_page = anon_client.get("/login")
        assert t("help_line", "it") in anon_page.text, \
            "18: the clinic line must appear on an anonymous GET with no error"

        cf_help_wrong = "MRSS950010150800"
        seed_and_issue(cf_help_wrong)
        wrong_help_client = app.test_client()
        wrong_help_page = wrong_help_client.get("/login")
        wrong_help_resp = wrong_help_client.post("/login", data={
            "codice_fiscale": cf_help_wrong, "pin": "00000000",
            "csrf_token": _csrf_from(wrong_help_page.text),
        })
        assert t("help_line", "it") in wrong_help_resp.text, \
            "18: the clinic line must appear on a wrong-pin refusal"

        unknown_help_client = app.test_client()
        unknown_help_page = unknown_help_client.get("/login")
        unknown_help_resp = unknown_help_client.post("/login", data={
            "codice_fiscale": "ZZZZ888888888888", "pin": "00000000",
            "csrf_token": _csrf_from(unknown_help_page.text),
        })
        assert t("help_line", "it") in unknown_help_resp.text, \
            "18: the clinic line must appear on an unknown-cf refusal"

        en_anon_client = app.test_client()
        en_anon_client.get("/lang/en", follow_redirects=True)
        en_anon_page = en_anon_client.get("/login")
        assert t("help_line", "en") in en_anon_page.text, \
            "18: the clinic line must appear in english too"

        # structural: help_line must never be reachable inside a jinja
        # conditional - the simplest honest form is to assert the template
        # contains no {% if tag at all. this pins D-02's "unconditional"
        # property against a later edit that wraps the line in a condition.
        login_template_text = Path("patient_app/templates/patient_login.html").read_text()
        assert "{% if" not in login_template_text, \
            "18: patient_login.html must contain no jinja conditional - " \
            "help_line must render unconditionally"

        # WR-05: the home screen has its own copy, not the login screen's
        cf_home = "BLLN960010150900"
        pin_home = seed_and_issue(cf_home)
        home_client, _ = sign_in(cf_home, pin_home)
        home_change_page = home_client.get("/change-pin")
        home_client.post("/change-pin", data={
            "pin": "97531864", "confirm": "97531864",
            "csrf_token": _csrf_from(home_change_page.text),
        })
        home_resp = home_client.get("/")
        assert t("home_heading", "it") in home_resp.text, \
            "18: the home screen should carry its own heading"
        assert t("login_heading", "it") not in home_resp.text, \
            "18: the home screen must not read like the login screen"

        # WR-13: an unmapped verify_pin status fails closed, not 500
        original_verify_pin = patient_auth.verify_pin
        patient_auth.verify_pin = lambda cf, pin, conn, now=None, ip=None: ("no_such_status", None)
        try:
            unmapped_client = app.test_client()
            unmapped_page = unmapped_client.get("/login")
            unmapped_resp = unmapped_client.post("/login", data={
                "codice_fiscale": "ANYTHING", "pin": "00000000",
                "csrf_token": _csrf_from(unmapped_page.text),
            })
            assert unmapped_resp.status_code == 200, \
                "18: an unmapped verify_pin status must not 500 the login form"
            assert t("err_bad_credentials", "it") in unmapped_resp.text, \
                "18: an unmapped status should fail closed to the generic refusal"
        finally:
            patient_auth.verify_pin = original_verify_pin

        # WR-09: one language helper, not two definitions that happen to agree
        assert patient_app.current_language is patient_routes.current_language, \
            "18: patient_app and patient_app.routes must share the same current_language function object"

        # 19. D-04/D-06 - the login route threads the source address, and a
        # throttled source renders the generic refusal like every other
        # refusal. fails until the route passes ip=request.remote_addr into
        # verify_pin and STATUS_TO_ERROR_KEY maps "throttled".
        reset19 = raw_db()
        reset19.execute("DELETE FROM patient_login_attempts")
        reset19.commit()
        reset19.close()

        cf19 = "FBRZ970010150900"
        seed_and_issue(cf19)
        ip_client = app.test_client()
        page19 = ip_client.get("/login")
        ip_client.post("/login", data={
            "codice_fiscale": cf19, "pin": "00000000", "csrf_token": _csrf_from(page19.text),
        })
        newest19 = raw_db().execute(
            "SELECT ip FROM audit_log WHERE action = 'patient_pin_check' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert newest19["ip"] == "127.0.0.1", \
            f"19: the login route should thread request.remote_addr into the audit row, got " \
            f"{dict(newest19) if newest19 else None}"

        reset19b = raw_db()
        reset19b.execute("DELETE FROM patient_login_attempts")
        reset19b.commit()
        reset19b.close()

        throttle_client = app.test_client()
        for i in range(patient_auth.PATIENT_IP_ATTEMPT_THRESHOLD):
            tcf19 = f"THRT{i:012d}"
            seed_and_issue(tcf19, name="throttle patient")
            page = throttle_client.get("/login")
            throttle_client.post("/login", data={
                "codice_fiscale": tcf19, "pin": "00000000", "csrf_token": _csrf_from(page.text),
            })

        page19b = throttle_client.get("/login")
        throttled_resp = throttle_client.post("/login", data={
            "codice_fiscale": "GNTZ980010151000", "pin": "00000000",
            "csrf_token": _csrf_from(page19b.text),
        })
        assert throttled_resp.status_code == 200, "19: a throttled source should re-render the login form"
        assert t("err_bad_credentials", "it") in throttled_resp.text, \
            "19: a throttled source should render the generic refusal"
        assert t("err_locked", "it") not in throttled_resp.text, \
            "19: a throttled source must not render the locked copy"
        assert t("err_expired", "it") not in throttled_resp.text, \
            "19: a throttled source must not render the expired copy"

        # reset the throttle before this section - see the comment above
        # section 13
        reset20 = raw_db()
        reset20.execute("DELETE FROM patient_login_attempts")
        reset20.commit()
        reset20.close()

        # 20. D-08's route-level proof - reissue actually recovers something.
        # a signed-in patient whose credential is reissued by staff must be
        # bounced to /login on their very next request. a service-level
        # assertion alone (patient_auth.py section 15) is what let CR-02 ship
        # in the first place (17-REVIEW.md).
        cf20 = "MNTV930010151600"
        pin20 = seed_and_issue(cf20)
        client20, _ = sign_in(cf20, pin20)
        change_page20 = client20.get("/change-pin")
        client20.post("/change-pin", data={
            "pin": "15935728", "confirm": "15935728",
            "csrf_token": _csrf_from(change_page20.text),
        })
        still_ok = client20.get("/")
        assert still_ok.status_code == 200, "20: setup - the session should still work before reissue"

        raw_conn20 = raw_db()
        patient_auth.issue_pin(cf20, raw_conn20, "test-dentist", "dentist")
        raw_conn20.close()

        bounced = client20.get("/")
        assert bounced.status_code == 302 and "login" in bounced.headers["Location"], \
            "20: a session must be rejected on the next request after its credential is reissued"

        # 21. D-09/CR-01/WR-04 route-level proof. written first, observed
        # failing (17.1-05). the route must require the current pin for a
        # voluntary change, rotate the acting session's token rather than
        # bounce to login, and show the current-pin field only outside the
        # forced flow.
        reset21 = raw_db()
        reset21.execute("DELETE FROM patient_login_attempts")
        reset21.commit()
        reset21.close()

        # 21a - the acting session survives a voluntary change with a
        # rotated token, and exactly one session row remains
        cf21 = "PNRT940010152100"
        pin21 = seed_and_issue(cf21)
        client21, _ = sign_in(cf21, pin21)
        change_page21 = client21.get("/change-pin")
        client21.post("/change-pin", data={
            "pin": "51739246", "confirm": "51739246",
            "csrf_token": _csrf_from(change_page21.text),
        })
        old_token21 = client21.get_cookie(patient_auth.PATIENT_COOKIE_NAME).value

        voluntary_page21 = client21.get("/change-pin")
        voluntary_resp21 = client21.post("/change-pin", data={
            "current": "51739246", "pin": "62840357", "confirm": "62840357",
            "csrf_token": _csrf_from(voluntary_page21.text),
        })
        assert voluntary_resp21.status_code == 302 and voluntary_resp21.headers["Location"] == "/", \
            "21: a completed voluntary change should redirect home"
        new_token21 = client21.get_cookie(patient_auth.PATIENT_COOKIE_NAME).value
        assert new_token21 != old_token21, \
            "21: the acting session's token must be rotated by a successful change"
        assert client21.get("/").status_code == 200, \
            "21: the same browser should stay signed in on the rotated token"
        assert count(
            "SELECT COUNT(*) FROM patient_sessions WHERE codice_fiscale = ?", (cf21,)
        ) == 1, "21: exactly one session row should remain for this patient"

        # 21b - the three messages reach the screen for a voluntary change
        cf21b = "MSSG950010152200"
        pin21b = seed_and_issue(cf21b)
        client21b, _ = sign_in(cf21b, pin21b)
        change_page21b = client21b.get("/change-pin")
        client21b.post("/change-pin", data={
            "pin": "84627193", "confirm": "84627193",
            "csrf_token": _csrf_from(change_page21b.text),
        })

        weak_page21b = client21b.get("/change-pin")
        weak_resp21b = client21b.post("/change-pin", data={
            "current": "84627193", "pin": "11111111", "confirm": "11111111",
            "csrf_token": _csrf_from(weak_page21b.text),
        })
        assert t("err_pin_weak", "it") in weak_resp21b.text, \
            "21: a weak new pin should render err_pin_weak"
        assert t("err_pin_short", "it", n=8) not in weak_resp21b.text, \
            "21: a weak-but-correct-length pin must not render the short message"

        same_page21b = client21b.get("/change-pin")
        same_resp21b = client21b.post("/change-pin", data={
            "current": "84627193", "pin": "84627193", "confirm": "84627193",
            "csrf_token": _csrf_from(same_page21b.text),
        })
        assert t("err_pin_same", "it") in same_resp21b.text, \
            "21: reusing the current pin should render err_pin_same"

        # 21c - the current-pin field is on the voluntary screen and absent
        # from the forced one
        cf21c = "FLDR960010152300"
        pin21c = seed_and_issue(cf21c)
        client21c, _ = sign_in(cf21c, pin21c)
        forced_screen21c = client21c.get("/change-pin")
        assert 'name="current"' not in forced_screen21c.text, \
            "21: the forced change screen must not show a current-pin field"
        client21c.post("/change-pin", data={
            "pin": "73951628", "confirm": "73951628",
            "csrf_token": _csrf_from(forced_screen21c.text),
        })
        voluntary_screen21c = client21c.get("/change-pin")
        assert 'name="current"' in voluntary_screen21c.text, \
            "21: the voluntary change screen must show a current-pin field"

        # 22. CR-06 - the patient auth cookie and the csrf session cookie both
        # carry Secure unless PATIENT_COOKIE_SECURE explicitly turns it off.
        # source-level checks plus the Set-Cookie header text, since this
        # suite has no TLS harness. fails until patient_auth.COOKIE_SECURE
        # exists and drives both writers.
        import importlib
        import os

        assert patient_auth.COOKIE_SECURE is True, \
            "22: COOKIE_SECURE must default to True with no env override set"
        assert app.config["SESSION_COOKIE_SECURE"] is True, \
            "22: the csrf session cookie must be secure by default too"

        cf22 = "SCRD970010152400"
        pin22 = seed_and_issue(cf22)
        secure_client = app.test_client()
        secure_page = secure_client.get("/login")
        secure_resp = secure_client.post("/login", data={
            "codice_fiscale": cf22, "pin": pin22, "csrf_token": _csrf_from(secure_page.text),
        })
        login_set_cookie = secure_resp.headers.get("Set-Cookie", "")
        assert "Secure" in login_set_cookie, \
            "22: the login Set-Cookie header must carry Secure"
        assert "HttpOnly" in login_set_cookie, \
            "22: the login Set-Cookie header must still carry HttpOnly"
        assert "SameSite=Strict" in login_set_cookie, \
            "22: the login Set-Cookie header must still carry SameSite=Strict"

        change_page22 = secure_client.get("/change-pin")
        change_resp22 = secure_client.post("/change-pin", data={
            "pin": "48627193", "confirm": "48627193",
            "csrf_token": _csrf_from(change_page22.text),
        })
        rotate_set_cookie = change_resp22.headers.get("Set-Cookie", "")
        assert "Secure" in rotate_set_cookie, \
            "22: the change-pin rotation's Set-Cookie header must carry Secure too - " \
            "both writers, not just the one that was easy to reach"
        assert "HttpOnly" in rotate_set_cookie, \
            "22: the change-pin rotation's Set-Cookie header must carry HttpOnly"
        assert "SameSite=Strict" in rotate_set_cookie, \
            "22: the change-pin rotation's Set-Cookie header must carry SameSite=Strict"

        # the opt-out exists precisely so it can be turned off deliberately -
        # test it in both directions and always restore the environment
        os.environ["PATIENT_COOKIE_SECURE"] = "0"
        try:
            importlib.reload(patient_auth)
            assert patient_auth.COOKIE_SECURE is False, \
                "22: PATIENT_COOKIE_SECURE=0 must turn the constant off"
        finally:
            del os.environ["PATIENT_COOKIE_SECURE"]
            importlib.reload(patient_auth)
            assert patient_auth.COOKIE_SECURE is True, \
                "22: the constant must return to secure once the override is removed"

        # 23. WR-02/WR-03/WR-06/WR-11/WR-12 - written first, observed failing
        # (17.1-08). reset the throttle before this section: 23a's GET
        # /login and 23c's over-long-pin POST both still reach verify_pin in
        # the pre-fix state (the length guard this plan adds does not exist
        # yet), so this section draws on the shared per-ip budget like every
        # other one that logs in.
        reset23 = raw_db()
        reset23.execute("DELETE FROM patient_login_attempts")
        reset23.commit()
        reset23.close()

        # 23a. WR-02 - first boot creates the db directory and schema instead
        # of letting sqlite make an empty shadow database that shadows the
        # real one
        original_db_path = patient_routes.DB_PATH
        wr02_db_path = str(Path(tmp) / "wr02" / "nested" / "clinic.sqlite")
        patient_routes.DB_PATH = wr02_db_path
        try:
            wr02_app = create_patient_app(env_path=Path(tmp) / ".env.patient.wr02")
            assert Path(wr02_db_path).exists(), \
                "23a: create_patient_app should create the db file on first boot"
            wr02_conn = sqlite3.connect(wr02_db_path)
            wr02_cols = wr02_conn.execute("PRAGMA table_info(patient_credentials)").fetchall()
            wr02_conn.close()
            assert len(wr02_cols) > 0, \
                "23a: patient_credentials should exist with columns after first boot"
            wr02_client = wr02_app.test_client()
            wr02_resp = wr02_client.get("/login")
            assert wr02_resp.status_code == 200, \
                "23a: /login should render 200 on a freshly booted db, not 500"
        finally:
            patient_routes.DB_PATH = original_db_path

        # 23b. WR-03 - every request's sqlite connection is closed on
        # teardown, not left to refcounting to collect
        assert app.teardown_appcontext_funcs, \
            "23b: create_patient_app should register a teardown_appcontext handler"
        with app.test_request_context():
            teardown_conn = patient_routes.get_db()
        # the only assertion possible from outside the app: a connection
        # closed by the teardown raises on the next use
        try:
            teardown_conn.execute("SELECT 1")
            raise AssertionError("23b: the connection should be closed once the request context ends")
        except sqlite3.ProgrammingError:
            pass

        # 23c. WR-06 - a body cap, plus an over-long pin refused before it
        # reaches the key derivation function
        assert app.config["MAX_CONTENT_LENGTH"] == 64 * 1024, \
            "23c: MAX_CONTENT_LENGTH should cap request bodies at 64KB"

        cf23c = "LNGP980010152500"
        seed_and_issue(cf23c)
        long_pin_client = app.test_client()
        long_pin_page = long_pin_client.get("/login")
        long_pin_resp = long_pin_client.post("/login", data={
            "codice_fiscale": cf23c, "pin": "1" * 200,
            "csrf_token": _csrf_from(long_pin_page.text),
        })
        assert long_pin_resp.status_code == 200, \
            "23c: an over-long pin should re-render the login form, not 500"
        assert t("err_bad_credentials", "it") in long_pin_resp.text, \
            "23c: an over-long pin should render the generic refusal, refused before hashing"

        # 23d. WR-11 - paths anchored on the repo root, not the process cwd
        assert patient_app.VENDOR_ROOT.is_absolute(), \
            "23d: VENDOR_ROOT should be an absolute, repo-anchored path"
        assert str(patient_app.VENDOR_ROOT).endswith("app/static/vendor"), \
            "23d: VENDOR_ROOT should point at app/static/vendor"
        original_vendor_root = patient_app.VENDOR_ROOT
        original_cwd = os.getcwd()
        try:
            os.chdir(tmp)
            importlib.reload(patient_app)
            assert patient_app.VENDOR_ROOT == original_vendor_root, \
                "23d: VENDOR_ROOT must not change when the process cwd changes"
        finally:
            os.chdir(original_cwd)
            importlib.reload(patient_app)

        # 23e. WR-11 - the entrypoint is guarded, so importing patient_run
        # does not block forever inside app.run(). source-level, following
        # section 17's own precedent - importing the module to prove the
        # guard works is exactly what the guard exists to make unsafe, and a
        # test that starts a listening server does not belong in an offline
        # suite.
        run_text = Path("patient_run.py").read_text()
        assert 'if __name__ == "__main__":' in run_text, \
            "23e: patient_run.py should guard its entrypoint"
        assert not re.search(r"(?m)^app\.run\(", run_text), \
            "23e: app.run( must not sit unguarded at column 0"

        # 23f. WR-12 - the residual full-privilege handle is narrowed: no
        # patient-side module may name a staff table in a SQL clause.
        # patient_sessions must not trip this, which is why the patterns are
        # anchored on the keyword before the table name rather than the bare
        # word - this section pins today's clean state against a regression,
        # it is not expected to fail red.
        staff_table_patterns = [
            re.compile(p, re.IGNORECASE) for p in (
                r"FROM\s+users\b", r"JOIN\s+users\b", r"INTO\s+users\b", r"UPDATE\s+users\b",
                r"FROM\s+sessions\b", r"INTO\s+sessions\b", r"UPDATE\s+sessions\b",
                r"FROM\s+pending_actions\b", r"UPDATE\s+pending_actions\b",
            )
        ]
        # patient_accessor.py is a new top-level file section 27 needs on this
        # same list - the patient_app/**/*.py glob does not reach it, since it
        # lives outside that package.
        patient_side_files = list(Path("patient_app").rglob("*.py")) + [
            Path("patient_auth.py"), Path("patient_accessor.py"),
        ]
        offenders = []
        for path in patient_side_files:
            text = path.read_text()
            for pattern in staff_table_patterns:
                if pattern.search(text):
                    offenders.append((str(path), pattern.pattern))
        assert not offenders, \
            f"23f: patient-side SQL must never name a staff table, found {offenders}"

        # ==================================================================
        # sections 24-27: the chat surface proven at the request level - two
        # live sessions, real cookies, real csrf. plans 01 and 03 proved these
        # properties inside their own modules with stubs; this is the
        # automated counterpart to the live two-login walk in plan 18-06.
        # ==================================================================

        def rendered(key, lang, **kwargs):
            # jinja's autoescape turns an apostrophe into &#39; - several
            # english copies below carry one (deflect_body, refusal_body,
            # refusal_heading, chat_error_heading), so a raw t(key, lang)
            # can never be found inside rendered html for those keys. this
            # mirrors what jinja actually emits, so an "in resp.text" check
            # stays a real assertion instead of failing on punctuation.
            return str(html_escape(t(key, lang, **kwargs)))

        def seed_visit(cf, visit_date, procedures, next_appointment, source_path):
            c = raw_db()
            c.execute(
                "INSERT INTO visits (codice_fiscale, visit_date, procedures,"
                " next_appointment, source_path) VALUES (?, ?, ?, ?, ?)",
                (cf, visit_date, json.dumps(procedures), next_appointment, source_path),
            )
            c.commit()
            visit_id = c.execute(
                "SELECT id FROM visits WHERE source_path = ?", (source_path,)
            ).fetchone()["id"]
            c.close()
            return visit_id

        def seed_invoice(cf, visit_id, amount, description, line_index=0):
            c = raw_db()
            c.execute(
                "INSERT INTO invoices (codice_fiscale, visit_id, line_index, amount, description)"
                " VALUES (?, ?, ?, ?, ?)",
                (cf, visit_id, line_index, amount, description),
            )
            c.commit()
            c.close()

        def activate(client, pin, new_pin):
            # drives the forced first-login change so the returned client
            # holds a session past FORCED_CHANGE_ALLOWED, same shape sections
            # 12/16/20/21 already use one call at a time
            change_page = client.get("/change-pin")
            client.post("/change-pin", data={
                "pin": new_pin, "confirm": new_pin, "csrf_token": _csrf_from(change_page.text),
            })

        # the stub every section below reuses so none of them needs a running
        # ollama - captures the built prompt and returns fixed canned text.
        # restored to the real function once section 26c is done with it.
        captured_prompts = []

        def _stub_call_model(prompt, urlopen=None):
            captured_prompts.append(prompt)
            return "risposta di prova"

        original_call_model = patient_app.chat._call_model
        patient_app.chat._call_model = _stub_call_model

        reset24 = raw_db()
        reset24.execute("DELETE FROM patient_login_attempts")
        reset24.commit()
        reset24.close()

        # 24 fixture: one patient, a name, a phone, two visits each carrying
        # visit_date/procedures/next_appointment, and one invoice - enough
        # real data for every route to answer rather than refuse.
        cf24 = "QRRC970010153000"
        pin24 = seed_and_issue(cf24, name="quirino quercia")
        c24 = raw_db()
        c24.execute("UPDATE patients SET phone = ? WHERE codice_fiscale = ?", ("3315551000", cf24))
        c24.commit()
        c24.close()
        seed_visit(cf24, "2031-01-10", ["seal 11"], "2031-06-01", "24/n1.json")
        visit24b = seed_visit(cf24, "2031-02-15", ["crown 22"], "2031-06-01", "24/n2.json")
        seed_invoice(cf24, visit24b, 120.0, "porcelain crown consult")

        client24, _ = sign_in(cf24, pin24)
        activate(client24, pin24, "84610237")

        # 24. GET /chat with a live session renders the empty state - the
        # four example chips are present, no response region yet. GET /chat
        # with no session cookie hits the default-deny whitelist, not a
        # per-route check.
        chat_get24 = client24.get("/chat")
        assert chat_get24.status_code == 200, "24: GET /chat with a live session should return 200"
        for key in ("chat_example_1", "chat_example_2", "chat_example_3", "chat_example_4"):
            assert t(key, "it") in chat_get24.text, f"24: expected the {key} chip on the empty chat page"
        assert 'role="status"' not in chat_get24.text, \
            "24: the empty chat state should render no role=status element"

        anon_chat = app.test_client().get("/chat")
        assert anon_chat.status_code == 302 and "/login" in anon_chat.headers["Location"], \
            "24: GET /chat with no session should redirect to /login via the default-deny whitelist"

        # 24a. a typed question posts successfully with the extracted csrf
        # token and returns exactly one role=status element - the no-
        # accumulation property stated as a count, not an eyeball.
        page24a = client24.get("/chat")
        resp24a = client24.post("/chat", data={
            "question": t("chat_example_2", "it"), "csrf_token": _csrf_from(page24a.text),
        })
        assert resp24a.status_code == 200, "24a: a typed question POST should return 200"
        count24a = resp24a.text.count('role="status"')
        assert count24a == 1, f"24a: expected exactly one role=status element, got {count24a}"

        # 24b. a chip click posts two fields both named "question" - the
        # empty text input first, the clicked chip second. this fails red
        # against a request.form.get("question") implementation, which would
        # read whichever field the browser lists first and silently drop the
        # chip's text.
        page24b = client24.get("/chat")
        chip_text24b = t("chat_example_1", "it")
        resp24b = client24.post("/chat", data={
            "question": ["", chip_text24b], "csrf_token": _csrf_from(page24b.text),
        })
        assert resp24b.status_code == 200, "24b: a chip click should return 200"
        assert t("answer_heading", "it") in resp24b.text, \
            "24b: the chip's own question should be answered, not refused as empty"
        assert t("refusal_heading", "it") not in resp24b.text, \
            "24b: a chip click must not be treated as an empty question"

        # 24c. D-03, no history. the second response on the same session
        # carries its own answer and none of the first question's text or the
        # first answer's text. a reload afterwards returns to the empty state.
        # the first question is typed, not one of the four example chips -
        # those render on every page load regardless of state, so a chip's
        # own text would still be present on the second response and could
        # never prove this property either way.
        first_page24c = client24.get("/chat")
        first_q24c = "Quando torno?"
        first_resp24c = client24.post("/chat", data={
            "question": first_q24c, "csrf_token": _csrf_from(first_page24c.text),
        })
        assert t("answer_heading", "it") in first_resp24c.text, \
            "24c: setup - the first question should be answered"

        second_page24c = client24.get("/chat")
        second_resp24c = client24.post("/chat", data={
            "question": "qual e la capitale della Francia",
            "csrf_token": _csrf_from(second_page24c.text),
        })
        assert first_q24c not in second_resp24c.text, \
            "24c: the second response must not carry the first question's text"
        assert "risposta di prova" not in second_resp24c.text, \
            "24c: the second response must not carry the first answer's text"

        reload24c = client24.get("/chat")
        assert 'role="status"' not in reload24c.text, \
            "24c: a reload with no new POST should return to the empty state with no response region"

        # 24d. no |safe filter, and a seeded value with a raw <b> comes back
        # html-escaped. no live model needed: a dedicated echo-stub returns
        # the built prompt itself as the answer body, so the seeded invoice
        # description that reaches the prompt also reaches the rendered page.
        chat_template_text = Path("patient_app/templates/patient_chat.html").read_text()
        assert "|safe" not in chat_template_text, "24d: patient_chat.html must contain no |safe filter"

        def _echo_call_model(prompt, urlopen=None):
            captured_prompts.append(prompt)
            # the built prompt carries the "reply with exactly NOT_IN_RECORDS"
            # instruction verbatim - echoing it unmodified would trip step 8's
            # sentinel check and turn this into a refusal instead of an
            # answer, so that one substring is swapped out before echoing
            return prompt.replace("NOT_IN_RECORDS", "ECHOED_CONTEXT")

        cf24d = "XSSC980010153100"
        pin24d = seed_and_issue(cf24d, name="xss patient")
        visit24d = seed_visit(cf24d, "2031-01-01", ["seal 33"], "2031-05-01", "24d/n1.json")
        seed_invoice(cf24d, visit24d, 10.0, "<b>bold</b> filling")
        client24d, _ = sign_in(cf24d, pin24d)
        activate(client24d, pin24d, "15935780")

        page24d = client24d.get("/chat")
        patient_app.chat._call_model = _echo_call_model
        resp24d = client24d.post("/chat", data={
            "question": t("chat_example_3", "it"), "csrf_token": _csrf_from(page24d.text),
        })
        patient_app.chat._call_model = _stub_call_model
        assert "<b>bold</b>" not in resp24d.text, \
            "24d: a seeded <b> value must come back HTML-escaped, not raw"
        assert "&lt;b&gt;bold&lt;/b&gt;" in resp24d.text, \
            "24d: expected the escaped form of the seeded <b> value in the response"

        # ---- section 25: the two-session cross-patient regression (CHAT-04/SC2) ----

        reset25 = raw_db()
        reset25.execute("DELETE FROM patient_login_attempts")
        reset25.commit()
        reset25.close()
        captured_prompts.clear()

        def seed_route_patient(cf, name, phone, visit_date, next_appointment, procedure,
                                invoice_amount, invoice_description, source_tag):
            pin = seed_and_issue(cf, name=name)
            c = raw_db()
            c.execute("UPDATE patients SET phone = ? WHERE codice_fiscale = ?", (phone, cf))
            c.commit()
            c.close()
            visit_id = seed_visit(cf, visit_date, [procedure], next_appointment, f"{source_tag}/n1.json")
            seed_invoice(cf, visit_id, invoice_amount, invoice_description)
            return pin

        def login_and_activate(cf, pin, new_pin):
            client = app.test_client()
            page = client.get("/login")
            client.post("/login", data={
                "codice_fiscale": cf, "pin": pin, "csrf_token": _csrf_from(page.text),
            })
            activate(client, pin, new_pin)
            return client

        # distinctiveness is the whole assertion here - two patients sharing a
        # name token, a phone digit sequence, a procedure marker or an
        # invoice description would make a leak invisible.
        cf_p1 = "BRTL990010153200"
        cf_p2 = "SNMR000010153300"

        pin_p1 = seed_route_patient(
            cf_p1, "elio bertorelli", "3311110001", "2031-07-04", "2031-09-14",
            "seal 60614", 245.50, "reline appliance batch 71829", "p1chat",
        )
        pin_p2 = seed_route_patient(
            cf_p2, "nadia sanmarco", "3477770002", "2031-08-19", "2031-10-25",
            "crown 97701", 318.75, "retainer fitting batch 84523", "p2chat",
        )

        # the tooth-number markers stand in for "procedure code" - the raw
        # code word itself does not survive render_procedure's glossary
        # phrasing in every language, but the tooth number always does
        leak_values_p1 = ("bertorelli", "3311110001", "2031-07-04", "60614", "71829")
        leak_values_p2 = ("sanmarco", "3477770002", "2031-08-19", "97701", "84523")

        client_p1 = login_and_activate(cf_p1, pin_p1, "13571113")
        client_p2 = login_and_activate(cf_p2, pin_p2, "24682246")

        route_questions = {
            "next_appointment": t("chat_example_1", "it"),
            "invoices": t("chat_example_3", "it"),
            "demographics": t("chat_example_4", "it"),
            "visits": t("chat_example_2", "it"),
        }

        def ask_all_routes(client):
            responses = {}
            for route, question in route_questions.items():
                page = client.get("/chat")
                resp = client.post("/chat", data={
                    "question": question, "csrf_token": _csrf_from(page.text),
                })
                responses[route] = resp.text
            return responses

        # 25. each client asks the same four questions, one per route. quoting
        # roadmap SC2: two different patient accounts asking the same style of
        # question never see each other's data.
        resp_p1 = ask_all_routes(client_p1)
        resp_p2 = ask_all_routes(client_p2)

        for route, body in resp_p1.items():
            for leak in leak_values_p2:
                assert leak not in body, (
                    f"25: SC2 - patient one's {route} response must not carry patient two's "
                    f"{leak!r} (two different patient accounts asking the same style of "
                    "question never see each other's data)"
                )
        for route, body in resp_p2.items():
            for leak in leak_values_p1:
                assert leak not in body, (
                    f"25: SC2 - patient two's {route} response must not carry patient one's {leak!r}"
                )

        # 25a. the prompt capture - the assertion that matters most. the
        # accessor is what guarantees the scoping; this proves the guarantee
        # survives all the way to the string the model is actually handed.
        assert len(captured_prompts) == 8, \
            f"25a: expected 8 captured prompts (4 routes x 2 patients), got {len(captured_prompts)}"
        prompts_p1 = captured_prompts[:4]
        prompts_p2 = captured_prompts[4:]
        for prompt in prompts_p1:
            for leak in leak_values_p2:
                assert leak not in prompt, \
                    f"25a: patient two's {leak!r} leaked into a prompt built for patient one"
        for prompt in prompts_p2:
            for leak in leak_values_p1:
                assert leak not in prompt, \
                    f"25a: patient one's {leak!r} leaked into a prompt built for patient two"

        # 25b. neither client's request wrote a patient_scope_violation row -
        # a row here would mean the D-09 return assertion fired on a
        # correctly scoped query, a real defect and not a reassurance.
        violations25b = count(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'patient_scope_violation'"
        )
        assert violations25b == 0, (
            "25b: a patient_scope_violation row exists after correctly scoped queries - "
            f"got {violations25b} rows"
        )

        # 25c. negative control. without this, 25b's zero-count assertion
        # could pass simply because the mechanism does not work - this proves
        # the counter can move.
        mismatched_rows25c = [{"codice_fiscale": cf_p2, "value": "should be dropped"}]
        conn25c = raw_db()
        kept25c = patient_accessor._scope_rows(mismatched_rows25c, cf_p1, conn25c, "negative_control")
        conn25c.close()
        assert kept25c == [], f"25c: a mismatched row must be dropped, got {kept25c}"
        violations25c = count(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'patient_scope_violation'"
        )
        assert violations25c == 1, \
            f"25c: the negative control must write exactly one patient_scope_violation row, got {violations25c}"
        violation_row25c = raw_db().execute(
            "SELECT * FROM audit_log WHERE action = 'patient_scope_violation' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert violation_row25c["allowed"] == 0, "25c: the violation row must be allowed=0"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python patient_app_selftest.py --selftest")


if __name__ == "__main__":
    main()
