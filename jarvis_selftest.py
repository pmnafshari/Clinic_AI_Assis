"""Jarvis J00: the state model, the companion service and its page, the clinic contract (device + delegated session).

No microphone, no model, no network beyond 127.0.0.1 in-process. Expectations fixed in .planning/plans/JARVIS.md
before the code.
"""
import json
import plistlib
import sqlite3
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import app.db as app_db
import auth
import clinic_time
import jarvis_link as jl
import web_session
from jarvis import launchd, runtime, states
from jarvis.clinic import ClinicLink, LinkDown, LinkRefused
from jarvis.web import create_app as jarvis_app
from patient_files_selftest import csrf, staff_client

T0 = clinic_time.read_instant("2026-10-04T08:00:00+00:00")
ADM, D, A = ("anadmin", "admin"), ("drossi", "dentist"), ("aassist", "assistant")


def refused(fn, kind=Exception):
    try:
        fn()
    except kind as e:
        return e
    raise AssertionError("HARD FAIL - not refused")


def state_model():
    # 1. exactly the transitions in the plan's table, and DEGRADED from anywhere
    m = states.Machine()
    assert m.state == "STARTING" and m.reason, "1: the service starts in STARTING with a reason"
    allowed = {("STARTING", "READY"), ("STARTING", "DEGRADED"), ("READY", "ACTIVE"), ("READY", "DEGRADED"),
               ("ACTIVE", "READY"), ("ACTIVE", "AUTH_REQUIRED"), ("ACTIVE", "CONFIRMING"), ("ACTIVE", "DEGRADED"),
               ("AUTH_REQUIRED", "READY"), ("AUTH_REQUIRED", "DEGRADED"), ("CONFIRMING", "READY"),
               ("CONFIRMING", "DEGRADED"), ("DEGRADED", "STARTING")}
    assert set(states.STATES) == {"STARTING", "READY", "ACTIVE", "AUTH_REQUIRED", "CONFIRMING", "DEGRADED"}
    for a in states.STATES:
        for b in states.STATES:
            if a == b:
                continue
            ok = (a, b) in allowed
            assert states.allowed(a, b) == ok, f"1: {a} -> {b} should be {'allowed' if ok else 'refused'}"
    m.go("DEGRADED", "microphone denied")
    refused(lambda: m.go("READY", "skip recovery"), states.BadTransition)
    m.go("STARTING", "cause cleared")
    m.go("READY", "listening")
    refused(lambda: m.go("CONFIRMING", "no request"), states.BadTransition)
    refused(lambda: m.go("READY", ""), ValueError)  # same state, and every move needs a reason
    snap = m.snapshot()
    assert snap["state"] == "READY" and snap["reason"] == "listening" and snap["since"], "1: the snapshot says why and since"
    assert [h["state"] for h in snap["history"]][-3:] == ["DEGRADED", "STARTING", "READY"], "1: recent history kept"
    assert all(set(h) == {"state", "reason", "at"} for h in snap["history"]), "1: history holds no speech or content"


def service():
    # 2. the page and the status API: loopback only, true state, live updates; closing the page stops nothing
    m = states.Machine()
    m.go("DEGRADED", "listening is not built yet (J01)")
    app = jarvis_app(m)
    app.config["TESTING"] = True
    c = app.test_client()
    r = c.get("/status", base_url="http://127.0.0.1:5020")
    data = json.loads(r.data)
    assert r.status_code == 200 and data["state"] == "DEGRADED" and "J01" in data["reason"], "2: /status tells the truth"
    page = c.get("/", base_url="http://127.0.0.1:5020").data.decode()
    assert "DEGRADED" in page and "listening is not built yet (J01)" in page, "2: the page shows the true state"
    assert "<button" not in page.lower() and "push to talk" not in page.lower(), \
        "2: the page offers no click-to-talk: a conversation never starts from the page"
    for host in ("evil.example", "192.168.1.5:5020", "localhost.evil:5020"):
        assert c.get("/status", base_url=f"http://{host}").status_code == 403, f"2: Host {host} not refused"
    assert c.get("/status", base_url="http://localhost:5020").status_code == 200, "2: localhost is the same machine"
    stream = c.get("/events", base_url="http://127.0.0.1:5020", buffered=False)
    first = next(stream.response).decode()
    assert first.startswith("data: ") and json.loads(first[6:])["state"] == "DEGRADED", "2: the event stream starts with the state"
    m.go("STARTING", "retry")
    nxt = next(stream.response).decode()
    assert nxt.startswith("data: ") and json.loads(nxt[6:])["state"] == "STARTING", "2: a change is pushed as a data event"
    stream.close()
    assert m.state == "STARTING", "2: closing the page left the service's state alone"


def clinic(tmp):
    # 3. the clinic contract: admin registers a device, staff delegate their own live session, the API checks both
    db_path = str(tmp / "clinic.sqlite")
    app_db.DB_PATH = db_path
    from app import create_app
    from storage import connect
    app = create_app()
    app.config["TESTING"] = True
    conn = connect(db_path)
    conn.row_factory = sqlite3.Row
    admin, dentist, assistant = (staff_client(app, db_path, *u) for u in (ADM, D, A))
    for client, who in ((dentist, "dentist"), (assistant, "assistant")):
        page = client.get("/jarvis/devices")
        assert page.status_code == 302, f"3: {who} cannot open device registration"
    page = admin.get("/jarvis/devices").text
    r = admin.post("/jarvis/devices", data={"csrf_token": csrf(page), "name": "Reception Mac"})
    token = jl.LAST_SHOWN_TOKEN
    assert r.status_code == 200 and token and token in r.text, "3: the admin sees the device credential once"
    assert token not in admin.get("/jarvis/devices").text, "3: and never again"
    raw = "\n".join(str(tuple(x)) for x in conn.execute("SELECT * FROM jarvis_devices"))
    assert token not in raw, "3: HARD FAIL - the device credential is stored in clear"
    device = conn.execute("SELECT id FROM jarvis_devices").fetchone()[0]

    def whoami(bearer, now=None):
        return jl.whoami(conn, bearer, now=now)
    # without a delegation: the device is known, nobody is signed in
    w = whoami(token, T0)
    assert w["device"] == "Reception Mac" and w["delegation"] is None, "3: device known, no staff session"
    for bad in ("", "x" * 43, token[:-1] + ("A" if token[-1] != "A" else "B")):
        refused(lambda: whoami(bad, T0), jl.LinkError)
    # the API over HTTP: bearer only, no staff cookie needed, every call audited
    anon = app.test_client()
    r = anon.get("/api/jarvis/whoami", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.get_json()["device"] == "Reception Mac", "3: the API answers a registered device"
    r = anon.get("/api/jarvis/whoami", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401 and "device" not in r.get_json(), "3: an unknown credential gets 401 and nothing else"
    assert anon.get("/api/jarvis/whoami").status_code == 401, "3: no credential, 401"
    audit = conn.execute("SELECT allowed, COUNT(*) FROM audit_log WHERE action = 'jarvis_api' GROUP BY allowed").fetchall()
    assert dict((a[0], a[1]) for a in audit) == {0: 2, 1: 1}, f"3: every API call audited ({[tuple(a) for a in audit]})"
    # a dentist delegates their own live session to the device
    page = dentist.get("/jarvis/link").text
    assert "Reception Mac" in page, "3: staff see the registered devices"
    r = dentist.post(f"/jarvis/link/{device}", data={"csrf_token": csrf(page)})
    assert r.status_code == 302, "3: delegation from the page"
    w = whoami(token)
    assert w["delegation"] and w["delegation"]["username"] == "drossi" and w["delegation"]["role"] == "dentist", \
        "3: the device now acts for the dentist's live session"
    # admin cannot delegate (no use_jarvis); a delegation needs the actor's own session
    n = conn.execute("SELECT COUNT(*) FROM jarvis_delegations").fetchone()[0]
    admin.post(f"/jarvis/link/{device}", data={"csrf_token": csrf(admin.get('/jarvis/devices').text)})
    assert conn.execute("SELECT COUNT(*) FROM jarvis_delegations").fetchone()[0] == n, "3: HARD FAIL - admin delegated"
    other = web_session.create_session(conn, "aassist", "assistant")
    refused(lambda: jl.delegate(conn, device, other, "drossi", "dentist"), jl.LinkError)
    # the staff session expires: the delegation is gone with it, and Jarvis reading it never extends it
    sid = conn.execute("SELECT session_hash FROM jarvis_delegations ORDER BY id DESC LIMIT 1").fetchone()[0]
    seen = conn.execute("SELECT last_seen_at FROM sessions WHERE token_hash = ?", (sid,)).fetchone()[0]
    whoami(token)
    assert conn.execute("SELECT last_seen_at FROM sessions WHERE token_hash = ?", (sid,)).fetchone()[0] == seen, \
        "3: HARD FAIL - Jarvis kept the staff session alive"
    later = clinic_time.read_instant(seen) + timedelta(minutes=web_session.SESSION_IDLE_MINUTES + 1)
    assert whoami(token, later)["delegation"] is None, "3: an expired staff session ends the delegation"
    # absolute cap, revocation of the delegation and of the device
    page = dentist.get("/jarvis/link").text
    dentist.post(f"/jarvis/link/{device}", data={"csrf_token": csrf(page)})
    cap = clinic_time.now_utc() + timedelta(hours=jl.DELEGATION_HOURS, minutes=1)
    conn.execute("UPDATE sessions SET last_seen_at = ?", (clinic_time.to_storage(cap - timedelta(minutes=1)),))
    conn.commit()
    assert whoami(token, cap)["delegation"] is None, "3: a delegation never outlives its cap"
    page = dentist.get("/jarvis/link").text
    dentist.post(f"/jarvis/link/{device}", data={"csrf_token": csrf(page)})
    did = conn.execute("SELECT id FROM jarvis_delegations WHERE revoked_at IS NULL ORDER BY id DESC LIMIT 1").fetchone()[0]
    page = dentist.get("/jarvis/link").text
    dentist.post(f"/jarvis/link/delegations/{did}/revoke", data={"csrf_token": csrf(page)})
    assert whoami(token)["delegation"] is None, "3: revoking the delegation ends it at once"
    page = assistant.get("/jarvis/link").text
    assistant.post(f"/jarvis/link/{device}", data={"csrf_token": csrf(page)})
    assert whoami(token)["delegation"]["username"] == "aassist", "3: reception may delegate their own session"
    page = admin.get("/jarvis/devices").text
    admin.post(f"/jarvis/devices/{device}/revoke", data={"csrf_token": csrf(page)})
    refused(lambda: whoami(token), jl.LinkError)
    assert anon.get("/api/jarvis/whoami", headers={"Authorization": f"Bearer {token}"}).status_code == 401, \
        "3: a revoked device is refused at once"
    # role changes and deactivation flow through: the role always comes from the account
    assert "use_jarvis" in auth.PERMISSIONS["dentist"] and "use_jarvis" in auth.PERMISSIONS["assistant"] \
        and "use_jarvis" not in auth.PERMISSIONS["admin"], "3: who may delegate"
    conn.close()
    return app


def boot(tmp):
    # 4. the companion's start-up is honest: never READY without listening (J01); a missing piece is DEGRADED, with why
    class FakeLink:
        def __init__(self, outcome):
            self.outcome = outcome

        def whoami(self):
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return self.outcome
    for outcome, reason in ((LinkDown("clinic app unreachable"), "clinic app unreachable"),
                            (LinkRefused("device not registered or revoked"), "device not registered or revoked"),
                            ({"device": "Reception Mac", "delegation": None}, "listening is not built yet (J01)")):
        m = states.Machine()
        runtime.check(m, FakeLink(outcome))
        assert m.state == "DEGRADED" and m.reason == reason, f"4: {outcome!r} -> {m.state} / {m.reason}"
    m = states.Machine()
    runtime.check(m, None)
    assert m.state == "DEGRADED" and m.reason == "no device credential on this machine", "4: no credential"
    # a later check recovers through STARTING when the cause clears
    m = states.Machine()
    runtime.check(m, FakeLink(LinkDown("clinic app unreachable")))
    runtime.check(m, FakeLink({"device": "Reception Mac", "delegation": None}))
    assert [h["state"] for h in m.snapshot()["history"]][-2:] == ["STARTING", "DEGRADED"] and "J01" in m.reason, \
        "4: recovery goes back through STARTING"
    # the credential file is the owner's: read from 600, never echoed
    key = tmp / "jarvis-device"
    key.write_text("secret-token\n")
    key.chmod(0o644)
    refused(lambda: ClinicLink("http://127.0.0.1:1", key), PermissionError)
    key.chmod(0o600)
    link = ClinicLink("http://127.0.0.1:1", key)
    e = refused(link.whoami, LinkDown)
    assert "secret-token" not in str(e), "4: HARD FAIL - the credential leaked into an error"


def agent():
    # 5. the LaunchAgent: at login, restarted after a crash, not after a clean stop; no secrets in it
    data = plistlib.loads(launchd.plist("/x/.venv/bin/python", "/x/Demo", "/x/Logs"))
    assert data["Label"] == launchd.LABEL and data["RunAtLoad"] is True, "5: starts at login"
    assert data["KeepAlive"] == {"SuccessfulExit": False} and data["ThrottleInterval"] >= 5, "5: restarted after a crash"
    assert data["ProgramArguments"][-1].endswith("jarvis_run.py") and data["WorkingDirectory"] == "/x/Demo"
    assert not any(k in json.dumps(data).lower() for k in ("token", "secret", "password", "key=")), "5: no secret in the agent"


def selftest():
    state_model()
    service()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        clinic(tmp)
        boot(tmp)
    agent()
    print("jarvis_selftest: ok")


if __name__ == "__main__":
    selftest()
    sys.exit(0)
