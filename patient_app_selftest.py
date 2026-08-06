import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from flask import url_for

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
        patient_auth.verify_pin = lambda cf, pin, conn: ("no_such_status", None)
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

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python patient_app_selftest.py --selftest")


if __name__ == "__main__":
    main()
