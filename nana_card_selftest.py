"""Nana's card on the doctor's pages (owner, 2026-11-09): the clinic app shows the voice companion as a card at the
bottom of every page a dentist opens - on, standby (small, still listening) or off - and turning it on hands the
dentist's own signed-in session to this computer's Nana, so there is no second sign-in.

The service side: the clinic app's own pages (its address on this machine) may read Nana's status and live stream and
press its buttons (confirm, listening, recheck) - never its password sign-in; the status says which device this is
and, as soon as it is transcribed, what was asked in the current interaction. The clinic side: the card is on a
dentist's pages only; on/off delegates and ends the delegation, audited; nobody else can."""
import json
import sys
import tempfile
from pathlib import Path

from werkzeug.security import generate_password_hash

import jarvis_link as jl
import storage
import web_session
from jarvis import runtime, states
from jarvis.web import create_app as service_app

BASE = "http://127.0.0.1:5020"
CLINIC = "http://127.0.0.1:5000"
STAFF = ("http://127.0.0.1:5000", "http://localhost:5000", "http://[::1]:5000")


def service():
    m = states.Machine()
    m.go("READY", "listening")
    c = service_app(m, clinic_url=CLINIC).test_client()

    # 1. the clinic app's pages may read the status and the live stream; any other page may not
    for origin in STAFF:
        r = c.get("/status", base_url=BASE, headers={"Origin": origin})
        assert r.headers.get("Access-Control-Allow-Origin") == origin, f"1: {origin} cannot read the status"
    for origin in ("http://evil.example", "http://127.0.0.1:5001", "null"):
        r = c.get("/status", base_url=BASE, headers={"Origin": origin})
        assert "Access-Control-Allow-Origin" not in r.headers, f"1: HARD FAIL - {origin} may read Nana's status"
    assert c.get("/status", base_url=BASE).headers.get("Access-Control-Allow-Origin") is None, "1: no origin, no header"

    # 2. ...and press its buttons, after the browser's preflight; the password sign-in stays Nana's own page's
    pre = c.options("/listening", base_url=BASE, headers={"Origin": STAFF[1], "Access-Control-Request-Method": "POST",
                                                          "Access-Control-Request-Headers": "content-type"})
    assert pre.status_code in (200, 204) and pre.headers.get("Access-Control-Allow-Origin") == STAFF[1] and \
        "POST" in pre.headers.get("Access-Control-Allow-Methods", "") and \
        "content-type" in pre.headers.get("Access-Control-Allow-Headers", "").lower(), f"2: preflight {pre.headers}"
    r = c.post("/listening", base_url=BASE, json={"paused": True}, headers={"Origin": STAFF[1]})
    assert r.status_code == 204 and m.paused, "2: the card cannot switch listening off"
    c.post("/listening", base_url=BASE, json={"paused": False}, headers={"Origin": STAFF[0]})
    assert not m.paused, "2: the card cannot switch listening on"
    assert c.post("/listening", base_url=BASE, json={"paused": True}, headers={"Origin": "http://evil.example"}).status_code \
        == 403 and not m.paused, "2: HARD FAIL - another site switched listening"
    for path in ("/signin", "/handoff", "/signout"):
        r = c.post(path, base_url=BASE, json={"username": "x", "password": "y"}, headers={"Origin": STAFF[1]})
        assert r.status_code == 403, f"2: HARD FAIL - the clinic page reached {path}"
        pre = c.options(path, base_url=BASE, headers={"Origin": STAFF[1], "Access-Control-Request-Method": "POST"})
        assert "Access-Control-Allow-Origin" not in pre.headers, f"2: {path} is offered to the clinic page"
    t = m.begin("heard the wake phrase")
    pid = m.offer(t, "what is error E05", 30)
    r = c.post("/confirm", base_url=BASE, json={"id": pid, "decision": "discard"}, headers={"Origin": STAFF[2]})
    assert r.status_code == 204, "2: the card cannot answer Did I hear you right?"
    m.finish(t, "done")

    # 3. a recheck asks the link check to run now (after the card hands a session over or takes it back)
    assert not m.recheck.is_set()
    assert c.post("/recheck", base_url=BASE, headers={"Origin": "http://evil.example"}).status_code == 403
    assert c.post("/recheck", base_url=BASE, headers={"Origin": STAFF[1]}).status_code == 204 and m.recheck.is_set(), \
        "3: no recheck"

    # 4. the status says which device this is (the card hands the session to it) ...
    class Link:
        def whoami(self):
            return {"device": "Studio Mac", "device_id": 7, "delegation": None, "approved_guides": 1}
    runtime.check_link(m, Link())
    assert m.snapshot()["clinic"]["device_id"] == 7, "4: the device is not in the status"

    class Down:
        def whoami(self):
            from jarvis.clinic import LinkDown
            raise LinkDown("no clinic")
    runtime.check_link(m, Down())
    assert m.snapshot()["clinic"].get("device_id") is None, "4: a device id kept while the clinic is unreachable"

    # 5. ... and what was asked, as soon as it is transcribed: the card prints it, then thinks, then the answer
    t = m.begin("heard the wake phrase")
    snap = m.snapshot()
    assert snap["turn"] == t and snap["asked"] is None, "5: the turn, nothing asked yet"
    assert m.heard_for(t, "Show me Marco's next appointment.")
    assert m.snapshot()["asked"] == {"turn": t, "text": "Show me Marco's next appointment."}, "5: asked"
    assert not m.heard_for(t - 1, "old"), "5: an old interaction's words are shown"
    t2 = m.begin("heard the wake phrase")
    assert m.snapshot()["asked"] is None and m.snapshot()["turn"] == t2, "5: a new wake keeps the last question"
    # ...and how many interactions there have been: the card shows only those after it was turned on, never the last
    # answer still on Nana's screen (it may be another doctor's)
    before = m.snapshot()["turns"]
    m.finish(t2, "done")
    assert m.snapshot()["turns"] == before and m.snapshot()["turn"] == 0, "5: turns counts interactions, not states"
    m.begin("heard the wake phrase")
    assert m.snapshot()["turns"] == before + 1, "5: a new interaction is counted"
    print("service ok")


def exchange_prints_the_question():
    # 6. the exchange puts what was heard in the status before anything else is done with it
    from jarvis import answer

    class Link:
        def whoami(self):
            return {"device": "Studio Mac", "device_id": 7, "delegation": None}

        def vocabulary(self):
            return []

        def ask_guides(self, q):
            raise AssertionError("not reached")
    m = states.Machine()
    m.go("READY", "listening")
    seen = []

    def transcribe(pcm, terms, cancelled=None):
        return "what is error E05", "en"
    ex = answer.Exchange(m, transcribe, Link, enabled=True, confirm=True, notes=True)
    ex.confirm_seconds = 0.2
    t = m.begin("heard the wake phrase")
    orig = m.offer

    def offer(turn, heard, seconds):
        seen.append(m.snapshot()["asked"])
        return orig(turn, heard, seconds)
    m.offer = offer
    ex(b"", t)
    assert seen == [{"turn": t, "text": "what is error E05"}], f"6: not printed before the confirmation ({seen})"
    print("exchange ok")


def clinic():
    with tempfile.TemporaryDirectory() as tmp:
        import app.db as app_db
        from app import create_app
        db = str(Path(tmp) / "nana.sqlite")
        app_db.DB_PATH = db
        app_db.CHROMA_PATH = str(Path(tmp) / "chroma")
        conn = storage.init_db(db)
        for user, role in (("dr_nana", "dentist"), ("dr_two", "dentist"), ("desk", "assistant"), ("boss", "admin")):
            conn.execute("INSERT INTO users (username, password_hash, role, active) VALUES (?, ?, ?, 1)",
                         (user, generate_password_hash("x"), role))
        conn.commit()
        device, _token = jl.register_device(conn, "Studio Mac", "boss", "admin")
        gone, _t = jl.register_device(conn, "Old Mac", "boss", "admin")
        jl.revoke_device(conn, gone, "boss", "admin")
        app = create_app()
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False

        def client(user, role):
            c = app.test_client()
            token = web_session.create_session(conn, user, role)
            c.set_cookie(web_session.COOKIE_NAME, token)
            return c, token
        (dr, dr_token), (two, _), (desk, _), (boss, _) = (client("dr_nana", "dentist"), client("dr_two", "dentist"),
                                                          client("desk", "assistant"), client("boss", "admin"))

        # 7. the card is on every page a dentist opens - and on nobody else's, nor on the sign-in page
        for path in ("/", "/patients", "/appointments"):
            page = dr.get(path).get_data(as_text=True)
            assert 'id="nana"' in page and 'data-service="http://127.0.0.1:5020"' in page and \
                'data-user="dr_nana"' in page and "js/nana.js" in page, f"7: no card on {path}"
        for who in (desk, boss):
            assert 'id="nana"' not in who.get("/").get_data(as_text=True), "7: HARD FAIL - the card for a non-dentist"
        assert 'id="nana"' not in app.test_client().get("/login").get_data(as_text=True), "7: the card signed out"
        page = dr.get("/").get_data(as_text=True)
        assert 'data-phrase="Hey Jarvis"' in page, "7: the card does not say the wake phrase in use"
        assert 'role="log"' in page and 'aria-pressed="false"' in page, "7: the log and the on/off switch"

        # 8. on: the dentist's own live session goes to this computer's device - no password; off ends it
        r = dr.post("/jarvis/card", json={"on": True, "device_id": device})
        assert r.status_code == 200 and r.get_json() == {"on": True, "acting": "dr_nana"}, f"8: on {r.get_json()}"
        who = jl.current_delegation(conn, device)
        assert who and who["username"] == "dr_nana" and who["kind"] == "web", f"8: no delegation ({who})"
        row = conn.execute("SELECT session_hash FROM jarvis_delegations WHERE revoked_at IS NULL").fetchone()
        assert row[0] == web_session.token_hash(dr_token), "8: HARD FAIL - not the dentist's own session"
        dr.post("/jarvis/card", json={"on": True, "device_id": device})
        assert conn.execute("SELECT COUNT(*) FROM jarvis_delegations WHERE revoked_at IS NULL").fetchone()[0] == 1, \
            "8: two live delegations"
        two.post("/jarvis/card", json={"on": True, "device_id": device})
        assert jl.current_delegation(conn, device)["username"] == "dr_two", "8: the last doctor to turn it on"
        r = two.post("/jarvis/card", json={"on": False, "device_id": device})
        assert r.status_code == 200 and r.get_json() == {"on": False} and jl.current_delegation(conn, device) is None, \
            "8: off does not end it"
        dr.post("/jarvis/card", json={"on": True, "device_id": device})
        two.post("/jarvis/card", json={"on": False, "device_id": device})
        assert jl.current_delegation(conn, device)["username"] == "dr_nana", "8: HARD FAIL - off ended another's"
        dr.post("/jarvis/card", json={"on": False, "device_id": device})
        n = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action IN ('jarvis_delegate', 'jarvis_delegation_revoke')"
                         " AND allowed = 1").fetchone()[0]
        assert n == 6, f"8: not audited ({n})"

        # 9. refused: a non-dentist, a revoked or unknown device, a malformed request - with a reason, nothing changed
        for who in (desk, boss):
            r = who.post("/jarvis/card", json={"on": True, "device_id": device})
            assert r.status_code == 403 and jl.current_delegation(conn, device) is None, "9: HARD FAIL - a non-dentist"
        for bad in ({"on": True, "device_id": gone}, {"on": True, "device_id": 999}):
            r = dr.post("/jarvis/card", json=bad)
            assert r.status_code == 400 and r.get_json().get("error"), f"9: {bad} -> {r.status_code}"
        for bad in ({"on": "yes", "device_id": device}, {"on": True}, {"on": True, "device_id": "7"}, []):
            assert dr.post("/jarvis/card", json=bad).status_code == 400, f"9: {bad} accepted"
        assert jl.current_delegation(conn, device) is None

        # 10. in Italian
        dr.set_cookie("staff_lang", "it")
        page = dr.get("/").get_data(as_text=True).split('id="nana"', 1)[1]
        assert "Accendi Nana" in page, "10: the card is not in Italian"
        conn.close()
    print("clinic ok")


def selftest():
    service()
    exchange_prints_the_question()
    clinic()
    print(json.dumps({"nana_card_selftest": "ok"}))


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        print("usage: python nana_card_selftest.py --selftest")
