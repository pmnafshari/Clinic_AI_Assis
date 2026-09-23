"""The operator surface through the real routes (P11.06, T4): everything off,
the stop switch reception may hit, the re-arm only a dentist may, and a page
that leaks no secret and claims nothing is live."""
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

from werkzeug.security import generate_password_hash

import app.db as app_db
import providers
import web_session
from app import create_app


def _client(app, db_path, username, role):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR IGNORE INTO users (username, password_hash, role, active)"
                 " VALUES (?, ?, ?, 1)", (username, generate_password_hash("x"), role))
    conn.commit()
    token = web_session.create_session(conn, username, role)
    conn.close()
    client = app.test_client()
    client.set_cookie(web_session.COOKIE_NAME, token)
    return client


def _csrf(html):
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "clinic.sqlite")
        app_db.DB_PATH = db_path
        app_db.CHROMA_PATH = str(Path(tmp) / "chroma")
        app = create_app()
        app.config["TESTING"] = True
        dentist = _client(app, db_path, "pv_dentist", "dentist")
        reception = _client(app, db_path, "pv_assist", "assistant")

        # 1. the page says the outside world is unreachable, and why
        page = reception.get("/providers").text
        assert "Nothing can reach the outside world" in page
        assert "blocked: no adapter configured" in page, "1: both kinds blocked"
        # P12 added telephony as a third kind behind the same gate
        assert page.count("connector: disabled") == 3, "1: messaging, payments, phone all disabled"
        assert "Phone line" in page and "Not answering" in page, "1: the line says it is off"
        assert "there is no phone number" in page, "1: and that no call can arrive"
        assert "no sweep is scheduled" in page, "1: it does not promise a sweep that is not there"
        assert "Nothing has been attempted." in page

        # 2. it must not claim anything is live, and must leak no secret.
        # csrf is stripped first - it is a token by name, it appears in the
        # body tag as X-CSRFToken as well as in every form, and a check that
        # trips on it tells you nothing about provider secrets. removing only
        # the word leaves any OTHER token on the page to be caught.
        # scoped to <main>, which is this page's own output. the surrounding
        # chrome carries a "Change password" link and a tokens.css stylesheet,
        # and a scan that trips on those says nothing about provider secrets.
        body = re.search(r"<main\b[^>]*>(.*)</main>", page, flags=re.S).group(1)
        scan = re.sub(r"csrf[_-]?token", "", body, flags=re.I).lower()
        for leak in ("sandbox-not-a-secret", "https://sandbox", "secret", "token", "api_key",
                     "password", "bearer"):
            assert leak not in scan, f"2: the page leaks {leak!r}"
        # the page is allowed - required, in fact - to say "no live connector
        # exists". what it must never do is assert that one IS live.
        for claim in (r"connector:\s*live", r"mode:\s*live", r"live:\s*(yes|true|on)",
                      r"\blive\s+provider\b", r"\bis\s+live\b"):
            assert not re.search(claim, scan), f"2: the page claims {claim!r}"
        assert "no live connector exists" in scan, "2: and it must say the opposite outright"

        # 3. accepted is explained as not-arrived; unknown as not-retried
        assert "not the same as arrived" in page, "3: accepted is qualified"
        assert "nothing is re-sent on its own" in page, "3: unknown is qualified"

        # 4. reception may STOP the outside world without asking
        reception.post("/providers/messaging/kill", data={"csrf_token": _csrf(page)})
        page = reception.get("/providers").text
        assert "stop switch ON" in page, "4: the switch shows as thrown"
        # the reason still reads "no adapter configured": the brakes are checked
        # in order and the first one wins, which is what the audit records too.
        # the kill switch surfacing as the reason is covered in providers 3,
        # where an adapter IS configured.
        assert "blocked: no adapter configured" in page, "4: still blocked, by the first brake"
        assert "Clear the stop" not in page, "4: reception is not offered the re-arm"

        # 5. ... and may not start it again. only a dentist.
        reception.post("/providers/messaging/rearm", data={"csrf_token": _csrf(page)})
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        assert conn.execute("SELECT killed FROM provider_switches WHERE kind = 'messaging'"
                            ).fetchone()[0] == 1, "5: RECEPTION RE-ARMED SENDING"
        dpage = dentist.get("/providers").text
        assert "Clear the stop" in dpage, "5: the dentist is offered it"
        dentist.post("/providers/messaging/rearm", data={"csrf_token": _csrf(dpage)})
        assert conn.execute("SELECT killed FROM provider_switches WHERE kind = 'messaging'"
                            ).fetchone()[0] == 0, "5: a dentist may"
        # and it is still blocked, by the brake underneath
        assert "blocked: no adapter configured" in dentist.get("/providers").text, \
            "5: clearing the stop does not make anything sendable"

        # 6. admin sees none of it
        admin = _client(app, db_path, "pv_admin", "admin")
        assert admin.get("/providers").status_code == 302, "6: admin has no operator page"
        assert "Outside connections" not in admin.get("/", follow_redirects=True).text
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'view_providers'"
                            " AND allowed = 0").fetchone()[0] == 1, "6: the refusal is audited"

        # 7. an unknown kind in the url is refused, not created
        reception.post("/providers/wishes/kill", data={"csrf_token": _csrf(page)})
        kinds = {r[0] for r in conn.execute("SELECT kind FROM provider_switches")}
        assert kinds <= set(providers.KINDS), f"7: a url created a switch: {kinds}"
        conn.close()

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python provider_routes_selftest.py --selftest")
    sys.exit(1)


if __name__ == "__main__":
    main()
