"""Jarvis J02: a spoken clinic-guide question, answered on screen with its citation - never a spoken reply.

The synthetic P24 library in temp stores; a fake speech-to-text child, fake clinic link and clock where the real ones would
need a model, a server or time. No microphone, no network beyond 127.0.0.1 in-process. Expectations fixed in
.planning/plans/JARVIS.md §11 before the code.
"""
import json
import sqlite3
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import numpy as np

import app.db as app_db
import clinic_guides as cg
import clinic_time
import guide_fixtures as gf
import jarvis_link as jl
from jarvis import answer, listen, states, stt
from jarvis.clinic import LinkDown, LinkRefused
from jarvis.web import create_app as jarvis_app
from patient_files_selftest import csrf, staff_client

T0 = clinic_time.read_instant("2026-10-07T08:00:00+00:00")
ADM, D, A = ("anadmin", "admin"), ("drossi", "dentist"), ("aassist", "assistant")


def library(tmp):
    cg.DB_PATH = str(tmp / "db" / "guides.sqlite")
    cg.STORE = tmp / "guides"
    cg.SLOT_DIR = tmp / "slots"
    (tmp / "db").mkdir(exist_ok=True)
    gconn = cg.connect()
    ids, devices = gf.load(gconn, gf.build(tmp / "library"))
    return gconn, ids, devices


def clinic(tmp):
    db_path = str(tmp / "clinic.sqlite")
    app_db.DB_PATH = db_path
    from app import create_app
    from storage import connect
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = True        # the real protection stays on; the device route needs no token
    conn = connect(db_path)
    conn.row_factory = sqlite3.Row
    admin = staff_client(app, db_path, *ADM)
    page = admin.get("/jarvis/devices").text
    admin.post("/jarvis/devices", data={"csrf_token": csrf(page), "name": "Reception Mac"})
    token = jl.LAST_SHOWN_TOKEN
    device = conn.execute("SELECT id FROM jarvis_devices").fetchone()[0]
    return app, conn, db_path, admin, token, device


def ask(client, token, question, **kw):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post("/api/jarvis/guides/ask", json={"question": question}, headers=headers, **kw)


def route(tmp):
    # 1. the device's guide route: a registered device only, every call audited without the question, the guide scope
    #    is reception's whatever the delegation, withdrawn and restricted pages honoured, no other route opened
    from app import WHITELIST_ENDPOINTS
    # checked first: a staff page on this list would break the later requests before they could be judged (M19)
    assert WHITELIST_ENDPOINTS == {"static", "shared", "auth.login", "jarvis.api_whoami", "jarvis.api_guides_ask"}, \
        f"1: only the device routes skip the staff login ({WHITELIST_ENDPOINTS})"
    gconn, ids, devices = library(tmp)
    app, conn, db_path, admin, token, device = clinic(tmp)
    anon = app.test_client()
    secret_question = "What does the B-PROG button do on the AX-200 zebra-crossing?"
    r = ask(anon, token, "What does the B-PROG button do on the AX-200?")
    data = r.get_json()
    assert r.status_code == 200 and data["outcome"] == "answer", f"1: a registered device gets an answer ({r.status_code} {data})"
    c = data["citations"][0]
    assert c["page"] == 2 and c["verified"] is True and "B-PROG" in c["passage"] and c["title"], "1: cited, verified passage"
    assert data["asked_as"] == "What does the B-PROG button do on the AX-200?", "1: what was asked is returned"
    for bearer in (None, "nope", "x" * 43):
        r = ask(anon, bearer, "What does STANDBY do on the AX-200?")
        assert r.status_code == 401 and "outcome" not in (r.get_json() or {}), f"1: HARD FAIL - {bearer!r} not refused"
    dentist = staff_client(app, db_path, *D)
    r = dentist.post("/api/jarvis/guides/ask", json={"question": "What does STANDBY do on the AX-200?"})
    assert r.status_code == 401, "1: a staff cookie alone is not a device"
    assert anon.get("/api/jarvis/guides/ask", headers={"Authorization": f"Bearer {token}"}).status_code == 405, \
        "1: only POST"
    for body in (b"not json", json.dumps({"question": 5}).encode(), json.dumps({"q": "x"}).encode(),
                 json.dumps({"question": "   "}).encode()):
        r = anon.post("/api/jarvis/guides/ask", data=body, content_type="application/json",
                      headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 400, f"1: a malformed request is refused ({body[:20]!r} -> {r.status_code})"
    r = ask(anon, token, "What does B-PROG do on the AX-200? " + "x" * 2000)
    assert r.status_code == 200 and len(r.get_json()["asked_as"]) <= 500, "1: a long question is cut, not refused"
    # the scope: reception's, with no delegation, a dentist's live delegation, or an expired one
    page = dentist.get("/jarvis/link").text
    dentist.post(f"/jarvis/link/{device}", data={"csrf_token": csrf(page)})
    assert jl.whoami(conn, token)["delegation"]["role"] == "dentist", "1: setup - a dentist delegated a live session"
    for label in ("delegated", "expired"):
        if label == "expired":
            conn.execute("UPDATE jarvis_delegations SET expires_at = ?", (clinic_time.to_storage(T0 - timedelta(hours=1)),))
            conn.commit()
        d = ask(anon, token, "When is the emergency kit checked?").get_json()
        assert d["outcome"] == "abstain" and d["reason"] == "not_for_role", \
            f"1: HARD FAIL - a dentist-only document answered through Jarvis ({label}: {d['outcome']} {d['reason']})"
        d = ask(anon, token, "What happens if I hold DRY and STANDBY for 10 seconds on the AX-200?").get_json()
        assert d["outcome"] == "abstain" and d["reason"] == "restricted", \
            f"1: HARD FAIL - a restricted page answered through Jarvis ({label}: {d['reason']})"
    # withdrawn: acts on the next question
    before = ask(anon, token, "Quanto dura la modalita Rampa sulla CL-5?").get_json()
    assert before["outcome"] == "answer", f"1: setup - the CL-5 manual answers ({before['reason']})"
    cg.withdraw(gconn, ids["cl5"], "superseded by the vendor", *D)
    after = ask(anon, token, "Quanto dura la modalita Rampa sulla CL-5?").get_json()
    assert after["outcome"] == "abstain" and after["citations"] == [], "1: HARD FAIL - a withdrawn manual still answered"
    # which device: a hands-free list to name, from the register only
    d = ask(anon, token, "What does the autoclave's P1 button do?").get_json()
    assert d["reason"] == "ask_device" and {"AX-200", "AX-300"} <= {x["model"] for x in d["devices"]}, \
        f"1: the device question lists the registered devices to name ({d})"
    assert all(set(x) == {"make", "model", "room"} for x in d["devices"]), "1: the list says only make, model, room"
    # a patient or clinical question: P24's refusal, never an answer
    d = ask(anon, token, "What is patient Mario Rossi's phone number?").get_json()
    assert d["outcome"] == "abstain" and d["reason"] == "patient_data" and d["citations"] == [], "1: patient data refused"
    d = ask(anon, token, "Should Mario Rossi have an OPG?").get_json()
    assert d["outcome"] == "abstain" and d["reason"] == "clinical", "1: a clinical decision refused"
    # audit: allowed and refused calls recorded, the question never
    ask(anon, token, secret_question)
    audit = "\n".join(str(tuple(x)) for x in conn.execute("SELECT * FROM audit_log"))
    asks = "\n".join(str(tuple(x)) for x in gconn.execute("SELECT * FROM asks"))
    assert "zebra" not in audit and "zebra" not in asks, "1: HARD FAIL - the question text was logged"
    rows = conn.execute("SELECT username AS actor, allowed FROM audit_log WHERE action = 'jarvis_api' AND target = 'guides_ask'").fetchall()
    assert any(r["allowed"] == 1 and r["actor"] == f"jarvis-device:{device}" for r in rows) and \
        any(r["allowed"] == 0 for r in rows), "1: allowed and refused device calls are both audited"
    # CSRF stays on everywhere else; the login whitelist grows by this one device route only
    page = admin.get("/jarvis/devices").text
    assert admin.post("/jarvis/devices", data={"name": "No token"}).status_code == 400, "1: other POSTs still need a token"
    # a revoked device is refused at once
    page = admin.get("/jarvis/devices").text
    admin.post(f"/jarvis/devices/{device}/revoke", data={"csrf_token": csrf(page)})
    assert ask(anon, token, "What does STANDBY do on the AX-200?").status_code == 401, "1: HARD FAIL - revoked device answered"
    conn.close()
    gconn.close()


def spoken_codes(tmp):
    # 2. spoken codes are matched to the approved library's own codes, nothing else is rewritten
    gconn, ids, devices = library(tmp)
    codes = jl.library_codes(gconn)
    assert {"B-PROG", "AX-200", "AX-300", "E05", "P1"} <= set(codes.values()), f"2: the library's codes ({sorted(codes.values())[:20]})"
    m = lambda t: jl.match_codes(t, codes)  # noqa: E731
    assert m("What does the B prog button do?") == "What does the B-PROG button do?", m("What does the B prog button do?")
    assert m("what does P1 do on the A X 300") == "what does P1 do on the AX-300", m("what does P1 do on the A X 300")
    assert m("What does error E 05 mean?") == "What does error E05 mean?", m("What does error E 05 mean?")
    assert m("what does error e zero five mean") == "what does error E05 mean", m("what does error e zero five mean")
    assert m("On the AX 200, how long does DRY run?") == "On the AX-200, how long does DRY run?"
    # words a full stop or a comma separates are never joined into one code (added after mutation run 1: M05 survived)
    assert m("Is it P. 1 or P2?") == "Is it P. 1 or P2?", m("Is it P. 1 or P2?")
    for same in ("How do I start a program?", "What does standby do?", "Which water should I put in the tank?",
                 "What does ZX 9 do?", "Call me at 2 30", "Is it at 200 degrees?"):
        assert m(same) == same, f"2: not a library code, left alone ({same!r} -> {m(same)!r})"
    # RP-05 is only in the phone script, still waiting for review: not a code Jarvis may match
    assert m("What does RP 01 say?") == "What does RP-01 say?", m("What does RP 01 say?")
    assert m("What does RP 05 say?") == "What does RP 05 say?", "2: codes come from approved documents only"
    gconn.close()


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Link:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.asked = result, error, []

    def ask_guides(self, question):
        self.asked.append(question)
        if self.error:
            raise self.error
        return self.result


ANSWER = {"asked_as": "What does the B-PROG button do?", "outcome": "answer", "reason": None, "message": "",
          "escalation": "", "device": {"make": "DemoMed", "model": "AX-200", "room": "Sterilisation room"},
          "citations": [{"source_id": 1, "title": "DemoMed AX-200 User manual", "edition": "2", "version": "2.1",
                         "language": "en", "page": 2, "passage": "B-PROG selects program B for wrapped instruments.",
                         "verified": True, "from_figure": False}],
          "warnings": [{"source_id": 1, "page": 3, "text": "WARNING: Never open the door while the pressure indicator is red."}]}


def exchange():
    # 3. one exchange: transcribe, ask over loopback, show the cited answer for a while; an unclear transcript is never
    #    asked; a refusal or a link fault is shown as it is, never an invented answer; no transcript in history
    clock = Clock()
    m = states.Machine()
    m.go("READY", "listening for the wake phrase")
    m.go("ACTIVE", "heard the wake phrase - listening to the request")
    heard = []

    def stt_ok(pcm):
        heard.append(pcm)
        return "what does the B prog button do", "en"
    link = Link(ANSWER)
    ex = answer.Exchange(m, stt_ok, lambda: link, clock=clock)
    pcm = (np.sin(np.arange(16000) / 5) * 3000).astype(np.int16)
    reason = ex(pcm)
    assert link.asked == ["what does the B prog button do"], f"3: the transcript is what is asked ({link.asked})"
    snap = m.snapshot()
    a = snap["answer"]
    assert a["outcome"] == "answer" and a["heard"] == "what does the B prog button do", "3: the answer card shows what was heard"
    assert a["citations"][0]["page"] == 2 and a["citations"][0]["verified"] and a["warnings"], "3: with its citation and warnings"
    assert "B prog" not in reason and "B-PROG" not in reason, f"3: the state's reason carries no speech ({reason})"
    assert all("prog" not in h["reason"] for h in snap["history"]), "3: HARD FAIL - speech in the state history"
    m.go("READY", reason)
    clock.t += answer.ANSWER_SECONDS - 1
    assert m.snapshot()["answer"] is not None, "3: the answer stays on screen for a while"
    clock.t += 2
    assert m.snapshot()["answer"] is None, "3: and then it is gone - held in memory only"
    # the next wake clears the old answer at once
    ex(pcm)
    m.go("READY", "answered")
    m.go("ACTIVE", "heard the wake phrase - listening to the request")
    assert m.snapshot()["answer"] is None, "3: a new wake clears the previous answer"
    # unclear: not asked
    link2 = Link(ANSWER)

    def unclear(pcm):
        raise stt.Unclear("could not make out what was said")
    reason = answer.Exchange(m, unclear, lambda: link2, clock=clock)(pcm)
    assert link2.asked == [] and m.snapshot()["answer"]["outcome"] == "unclear", "3: an unclear transcript is not asked"
    # refusals and faults are shown, never turned into an answer
    for error, outcome in ((LinkDown("clinic app unreachable"), "unavailable"),
                           (LinkRefused("device not registered or revoked"), "unavailable")):
        m.go("READY", reason)
        m.go("ACTIVE", "heard the wake phrase - listening to the request")
        reason = answer.Exchange(m, stt_ok, lambda: Link(error=error), clock=clock)(pcm)
        a = m.snapshot()["answer"]
        assert a["outcome"] == outcome and a["citations"] == [] and str(error) in a["message"], \
            f"3: a link fault is shown as it is ({a})"
    m.go("READY", reason)
    m.go("ACTIVE", "heard the wake phrase - listening to the request")
    answer.Exchange(m, stt_ok, lambda: None, clock=clock)(pcm)
    assert m.snapshot()["answer"]["outcome"] == "unavailable", "3: no device credential on this machine is said plainly"
    refusal = {**ANSWER, "outcome": "abstain", "reason": "clinical", "message": "The assistant does not make clinical decisions.",
               "escalation": "This is a clinical decision for the dentist.", "citations": [], "warnings": []}
    m.go("READY", "x")
    m.go("ACTIVE", "heard the wake phrase - listening to the request")
    answer.Exchange(m, stt_ok, lambda: Link(refusal), clock=clock)(pcm)
    a = m.snapshot()["answer"]
    assert a["outcome"] == "abstain" and a["reason"] == "clinical" and a["escalation"], "3: P24's refusal shown with where to go"


def listener_hands_over():
    # 4. the listener hands the heard request to the exchange, returns to READY with its reason, and keeps no audio
    clock = type("C", (), {"mono": 1000.0, "wall": 1_800_000_000.0})()

    def mono():
        return clock.mono

    def wall():
        return clock.wall

    speech = (np.sin(np.arange(1280) / 3) * 6000).astype(np.int16)
    quiet = (np.sin(np.arange(1280) / 7) * 40).astype(np.int16)

    class Det:
        def __init__(self):
            self.n = 0

        def feed(self, c):
            self.n += 1
            return self.n == 20

        def reset(self):
            pass

    class Src:
        def __init__(self, script):
            self.script, self.drained = list(script), 0

        def open(self):
            pass

        def read(self, timeout):
            if not self.script:
                raise StopIteration
            clock.mono += 0.08
            clock.wall += 0.08
            return self.script.pop(0)

        def close(self):
            pass

        def drain(self):
            self.drained += 1

    got = []

    def on_request(pcm):
        got.append(pcm.copy())
        return "answered on screen (DemoMed AX-200 User manual, page 2)"
    m = states.Machine()
    src = Src([quiet] * 30 + [speech] * 15 + [quiet] * 25)
    lst = listen.Listener(m, Det, lambda: src, mono=mono, wall=wall, on_request=on_request)
    lst.run(type("E", (), {"is_set": lambda self: False})())
    assert len(got) == 1 and len(got[0]) >= 15 * 1280, f"4: the whole request is handed over ({[len(g) for g in got]})"
    assert m.state == "READY" and "answered on screen" in m.reason, f"4: back to READY with the exchange's reason ({m.reason})"
    assert lst.request == [] and lst.heard_bytes == 0, "4: no request audio kept after the exchange"
    assert src.drained == 1, "4: audio queued while answering is dropped, not fed to the wake detector later"


def stt_child():
    # 5. speech to text runs in a short-lived child: audio over a pipe (never a file), Italian or English only,
    #    Whisper's usual confidence floors, a time limit; the model's memory leaves with the child
    calls = []

    def fake_run(out):
        def run(cmd, input=None, capture_output=None, timeout=None, **kw):
            calls.append({"cmd": cmd, "input": input, "timeout": timeout})
            if out == "timeout":
                import subprocess
                raise subprocess.TimeoutExpired(cmd, timeout)
            return type("R", (), {"returncode": 0 if out != "crash" else 1, "stdout": json.dumps(out).encode()
                                  if out not in ("crash",) else b"", "stderr": b"boom"})()
        return run
    pcm = (np.sin(np.arange(16000) / 5) * 3000).astype(np.int16)
    good = {"text": " What does the B prog button do? ", "language": "en",
            "segments": [{"no_speech_prob": 0.02, "avg_logprob": -0.2}]}
    text, lang = stt.transcribe(pcm, run=fake_run(good))
    assert (text, lang) == ("What does the B prog button do?", "en"), f"5: the transcript ({text!r}, {lang})"
    call = calls[-1]
    assert call["input"] == pcm.tobytes() and call["timeout"] and not any(str(a).endswith((".wav", ".raw", ".pcm"))
                                                                          for a in call["cmd"]), "5: audio over the pipe only"
    assert call["cmd"][1:3] == ["-m", "jarvis.stt"], f"5: a separate child process ({call['cmd']})"
    for out, why in (({"text": "", "language": "en", "segments": []}, "empty"),
                     ({"text": "uh", "language": "en", "segments": [{"no_speech_prob": 0.9, "avg_logprob": -0.3}]}, "no speech"),
                     ({"text": "blah blah", "language": "it", "segments": [{"no_speech_prob": 0.1, "avg_logprob": -1.4}]}, "low"),
                     ("timeout", "time"), ("crash", "crash")):
        try:
            stt.transcribe(pcm, run=fake_run(out))
        except stt.Unclear:
            pass
        else:
            raise AssertionError(f"5: {why} was accepted as a question")
    assert stt.pick_language({"en": 0.2, "it": 0.3, "ka": 0.5}) == "it", "5: the likelier of Italian and English"
    assert stt.pick_language({"en": 0.6, "it": 0.1}) == "en", "5: the likelier of Italian and English"


def page():
    # 6. the page shows the cited answer as text (never markup), and still offers no click-to-talk
    m = states.Machine()
    m.go("READY", "listening")
    m.go("ACTIVE", "heard the wake phrase - listening to the request")
    m.show({**{k: v for k, v in ANSWER.items()}, "heard": "<script>alert(1)</script> what does B prog do"}, 120)
    app = jarvis_app(m)
    c = app.test_client()
    html = c.get("/", base_url="http://127.0.0.1:5020").data.decode()
    assert "<script>alert(1)</script>" not in html, "6: HARD FAIL - heard text rendered as markup"
    assert "B-PROG selects program B" in html and "page 2" in html and "verified against the page" in html, \
        "6: the passage with its document, page and verification"
    assert "WARNING: Never open the door" in html, "6: the warnings with it"
    assert "innerHTML" not in html, "6: the live update writes text, never markup"
    assert "<button" not in html.lower(), "6: no click-to-talk"
    assert "not spoken" in html.lower() or "on screen" in html.lower(), "6: says answers are on screen, not spoken"
    status = json.loads(c.get("/status", base_url="http://127.0.0.1:5020").data)
    assert status["answer"]["citations"][0]["page"] == 2, "6: the status API carries the same answer"


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        route(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        spoken_codes(Path(tmp))
    exchange()
    listener_hands_over()
    stt_child()
    page()
    print("jarvis_guides_selftest: ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"] or not sys.argv[1:]:
        selftest()
    sys.exit(0)
