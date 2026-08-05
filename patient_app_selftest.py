import re
import sys
import tempfile
from pathlib import Path

import patient_auth
import web_session
from patient_app import create_patient_app
from patient_app.strings import DEFAULT_LANGUAGE, LANG_COOKIE_NAME, LANGUAGES, STRINGS, t

STAFF_ENDPOINTS = ("patients.detail_view", "auth.login", "qa.qa_page", "admin.users_view")


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        # its own secret file, so this never reads or writes the staff .env
        app = create_patient_app(env_path=Path(tmp) / ".env.patient")
        app.config["TESTING"] = True

        # 1. isolation, asserted rather than assumed
        endpoints = set(app.view_functions)
        for name in STAFF_ENDPOINTS:
            assert name not in endpoints, f"1: staff endpoint {name} must not exist on the patient app"
        assert patient_auth.PATIENT_COOKIE_NAME != web_session.COOKIE_NAME, \
            "1: the two apps must not share a cookie name"
        assert app.config["SESSION_COOKIE_NAME"] == patient_auth.PATIENT_COOKIE_NAME, \
            "1: the patient app must use its own cookie name"

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

        # 2. no CDN - offline-first, and this is the internet-facing surface
        home = client.get("/")
        assert home.status_code == 200, "2: the placeholder page should render"
        assert not re.search(
            r'<(?:script|link)[^>]+(?:src|href)="https?://', home.text, re.IGNORECASE
        ), "2: the patient surface must not reference any external asset"
        assert client.get("/vendor/bootstrap/5.3.3/bootstrap.min.css").status_code == 200, \
            "2: the vendored bootstrap should serve locally"

        # 3. italian is the default, not a fallback
        assert DEFAULT_LANGUAGE == "it"
        assert t("login_heading", "it") in home.text, "3: default page should be italian"
        assert t("login_heading", "en") not in home.text, "3: english must not leak into the default"

        # 4. the switch persists across requests - this is what proves the
        # cookie is carrying the choice. a page-scoped implementation passes
        # the first half of this and fails the second.
        switched = client.get("/lang/en", follow_redirects=True)
        assert t("login_heading", "en") in switched.text, "4: switching should render english"
        again = client.get("/")
        assert t("login_heading", "en") in again.text, \
            "4: the language choice must survive to the next page"
        client.get("/lang/it", follow_redirects=True)
        assert t("login_heading", "it") in client.get("/").text, "4: and switch back"

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

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python patient_app_selftest.py --selftest")


if __name__ == "__main__":
    main()
