"""Jarvis J02: a spoken clinic-guide question, answered on screen with its citation - never a spoken reply.

The synthetic P24 library in temp stores; a fake speech-to-text child, fake clinic link and clock where the real ones would
need a model, a server or time. No microphone, no network beyond 127.0.0.1 in-process. Expectations fixed in
.planning/plans/JARVIS.md §11 before the code; checks 7-10 (J-D10: the hint and the on-screen confirmation) in §12.
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
    assert WHITELIST_ENDPOINTS == {"static", "shared", "auth.login", "jarvis.api_whoami", "jarvis.api_guides_ask",
                                   "jarvis.api_guides_vocabulary"}, \
        f"1: only the device routes skip the staff login ({WHITELIST_ENDPOINTS})"
    gconn, ids, devices = library(tmp)
    app, conn, db_path, admin, token, device = clinic(tmp)
    anon = app.test_client()
    unlogged_question = "What does the B-PROG button do on the AX-200 zebra-crossing?"
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
    ask(anon, token, unlogged_question)
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
    def __init__(self, result=None, error=None, terms=("AX-200", "B-PROG", "DRY"), vocab_error=None):
        self.result, self.error, self.asked = result, error, []
        self.terms, self.vocab_error, self.calls = list(terms), vocab_error, []

    def vocabulary(self):
        self.calls.append("vocabulary")
        if self.vocab_error:
            raise self.vocab_error
        return list(self.terms)

    def ask_guides(self, question):
        self.calls.append("ask")
        self.asked.append(question)
        if self.error:
            raise self.error
        return self.result


def confirm_card(m, timeout=5.0):
    """Wait for the exchange to put what it heard on screen for confirmation. -> the card, or None."""
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        a = m.snapshot()["answer"]
        if a and a.get("outcome") == "confirm":
            return a
        time.sleep(0.01)
    return None


def run_exchange(ex, pcm, m, decide=None):
    """One exchange in its own thread, as the listener runs it; `decide(card)` plays the person at the screen.
    -> (the reason it returned, the confirmation card it showed or None)."""
    import threading
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("reason", ex(pcm)), daemon=True)   # a hung wait fails, not hangs
    t.start()
    card = confirm_card(m, timeout=3.0)
    if card is not None and decide is not None:
        decide(card)
    t.join(10)
    assert not t.is_alive(), "the exchange never finished"
    return out.get("reason"), card


ANSWER = {"asked_as": "What does the B-PROG button do?", "outcome": "answer", "reason": None, "message": "",
          "escalation": "", "device": {"make": "DemoMed", "model": "AX-200", "room": "Sterilisation room"},
          "citations": [{"source_id": 1, "title": "DemoMed AX-200 User manual", "edition": "2", "version": "2.1",
                         "language": "en", "page": 2, "passage": "B-PROG selects program B for wrapped instruments.",
                         "verified": True, "from_figure": False}],
          "warnings": [{"source_id": 1, "page": 3, "text": "WARNING: Never open the door while the pressure indicator is red."}]}


def exchange():
    # 3. one exchange: transcribe, ask over loopback once confirmed on screen, show the cited answer for a while; an
    #    unclear transcript is never asked; a refusal or a link fault is shown as it is, never an invented answer; no
    #    transcript in history (J02; since J-D10 the question is asked only after "Yes, ask this" - §12)
    clock = Clock()
    m = states.Machine()
    m.go("READY", "listening for the wake phrase")
    m.go("ACTIVE", "heard the wake phrase - listening to the request")
    heard = []

    def stt_ok(pcm, hint=None):
        heard.append(pcm)
        return "what does the B prog button do", "en"

    def yes(card):
        assert m.decide(card["id"], "ask"), "3: the confirmation is accepted"
    link = Link(ANSWER)
    ex = answer.Exchange(m, stt_ok, lambda: link, clock=clock, confirm_seconds=3)
    pcm = (np.sin(np.arange(16000) / 5) * 3000).astype(np.int16)
    reason, _card = run_exchange(ex, pcm, m, yes)
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
    m.go("ACTIVE", "heard the wake phrase - listening to the request")
    run_exchange(ex, pcm, m, yes)
    m.go("READY", "answered")
    m.go("ACTIVE", "heard the wake phrase - listening to the request")
    assert m.snapshot()["answer"] is None, "3: a new wake clears the previous answer"
    # unclear: not asked
    link2 = Link(ANSWER)

    def unclear(pcm, hint=None):
        raise stt.Unclear("could not make out what was said")
    reason = answer.Exchange(m, unclear, lambda: link2, clock=clock, confirm_seconds=3)(pcm)
    assert link2.asked == [] and m.snapshot()["answer"]["outcome"] == "unclear", "3: an unclear transcript is not asked"
    # refusals and faults are shown, never turned into an answer
    for error, outcome in ((LinkDown("clinic app unreachable"), "unavailable"),
                           (LinkRefused("device not registered or revoked"), "unavailable")):
        m.go("READY", reason)
        m.go("ACTIVE", "heard the wake phrase - listening to the request")
        reason, _card = run_exchange(answer.Exchange(m, stt_ok, lambda: Link(error=error), clock=clock, confirm_seconds=3),
                                     pcm, m, yes)
        a = m.snapshot()["answer"]
        assert a["outcome"] == outcome and a["citations"] == [] and str(error) in a["message"], \
            f"3: a link fault is shown as it is ({a})"
    m.go("READY", reason)
    m.go("ACTIVE", "heard the wake phrase - listening to the request")
    answer.Exchange(m, stt_ok, lambda: None, clock=clock, confirm_seconds=3)(pcm)
    assert m.snapshot()["answer"]["outcome"] == "unavailable", "3: no device credential on this machine is said plainly"
    refusal = {**ANSWER, "outcome": "abstain", "reason": "clinical", "message": "The assistant does not make clinical decisions.",
               "escalation": "This is a clinical decision for the dentist.", "citations": [], "warnings": []}
    m.go("READY", "x")
    m.go("ACTIVE", "heard the wake phrase - listening to the request")
    run_exchange(answer.Exchange(m, stt_ok, lambda: Link(refusal), clock=clock, confirm_seconds=3), pcm, m, yes)
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
    got_pcm, got_hint = stt.read_request(call["input"])
    assert np.array_equal(got_pcm, pcm) and got_hint == [] and call["timeout"] and \
        not any(str(a).endswith((".wav", ".raw", ".pcm")) for a in call["cmd"]), "5: audio over the pipe only"
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
    assert "<button" not in html.lower(), "6: no click-to-talk (with nothing waiting for confirmation, no button at all)"
    assert "not spoken" in html.lower() or "on screen" in html.lower(), "6: says answers are on screen, not spoken"
    status = json.loads(c.get("/status", base_url="http://127.0.0.1:5020").data)
    assert status["answer"]["citations"][0]["page"] == 2, "6: the status API carries the same answer"


def vocab(client, token):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.get("/api/jarvis/guides/vocabulary", headers=headers)


def plant(gconn, sid, page, words, column="text"):
    """Test-only: a word on one stored page, to see where the hint may and may not take it from. Pages are never
    rewritten in the app (a trigger), so the trigger is lifted for this one temp-store write and put back."""
    trigger = gconn.execute("SELECT sql FROM sqlite_master WHERE name = 'pages_text_fixed'").fetchone()[0]
    gconn.execute("DROP TRIGGER pages_text_fixed")
    n = gconn.execute(f"UPDATE pages SET {column} = {column} || ? WHERE source_id = ? AND page = ?",
                      (" " + words, sid, page)).rowcount
    gconn.execute(trigger)
    gconn.commit()
    assert n == 1, f"setup: page {page} of source {sid} not found"


def vocabulary_route(tmp):
    # 7. the speech-to-text hint (J-D10): terms from the current, approved library the device's guide scope may read -
    #    never a superseded, withdrawn, pending, dentist-only or restricted page, an unreadable page's OCR, a patient's
    #    name or anything shaped like a codice fiscale; computed on every call; audited without its content
    gconn, ids, devices = library(tmp)
    app, conn, db_path, admin, token, device = clinic(tmp)
    anon = app.test_client()
    plant(gconn, ids["ax200_v1"], 1, "OLDKEY-1")                      # only on the superseded edition
    plant(gconn, ids["cl5"], 1, "WDKEY-7")                            # withdrawn below, during the test
    plant(gconn, ids["cl5"], 5, "OCRKEY-3", "ocr_text")               # only in an unreadable page's OCR
    plant(gconn, ids["ax200_v2"], 2, "MARIO ROSSI KEEPME BNCLRA85M41F205X")   # a patient's name, a label, a CF shape
    conn.execute("INSERT INTO patients (patient_id, codice_fiscale, patient_name, phone) VALUES (?, ?, ?, ?)",
                 ("pid_vocab0000000001", "RSSMRA80A01H501U", "Mario Rossi", None))
    conn.commit()
    r = vocab(anon, token)
    assert r.status_code == 200 and r.headers.get("Cache-Control") == "no-store", f"7: a device gets the terms ({r.status_code})"
    terms = r.get_json()["terms"]
    assert {"AX-200", "B-PROG", "E05", "DRY", "STANDBY", "KEEPME", "WDKEY-7"} <= set(terms), \
        f"7: codes and labels from approved staff pages ({terms})"
    for absent, why in (("OLDKEY-1", "a superseded edition"), ("CP-09", "a dentist-only document"),
                        ("MENU", "restricted pages"), ("SERVICE", "restricted pages"),
                        ("RP-05", "a document still pending review"), ("OCRKEY-3", "an unreadable page's OCR"),
                        ("ROSSI", "a patient's name"), ("MARIO", "a patient's name"),
                        ("BNCLRA85M41F205X", "codice-fiscale-shaped text")):
        assert absent not in terms, f"7: HARD FAIL - {absent} from {why} is in the hint"
    assert terms == sorted(terms) and len(set(terms)) == len(terms), "7: sorted, each term once"
    # a code is one term, never also its pieces (added after development run 1 hinted AX, CL, CP, RP and PROG)
    assert not {"AX", "CL", "CP", "RP", "PROG", "LOW", "POWER"} & set(terms), f"7: pieces of codes in the hint ({terms})"
    for bearer in (None, "nope", "x" * 43):
        r = vocab(anon, bearer)
        assert r.status_code == 401 and "terms" not in (r.get_json() or {}), f"7: HARD FAIL - {bearer!r} not refused"
    dentist = staff_client(app, db_path, *D)
    assert vocab(dentist, None).status_code == 401, "7: a staff cookie alone is not a device"
    page = dentist.get("/jarvis/link").text
    dentist.post(f"/jarvis/link/{device}", data={"csrf_token": csrf(page)})
    assert jl.whoami(conn, token)["delegation"]["role"] == "dentist", "7: setup - a dentist delegated a live session"
    terms = vocab(anon, token).get_json()["terms"]
    assert "CP-09" not in terms and "MENU" not in terms, "7: HARD FAIL - a dentist's delegation widened the hint"
    # computed on every call: a withdrawal acts on the next one
    cg.withdraw(gconn, ids["cl5"], "superseded by the vendor", *D)
    assert "WDKEY-7" not in vocab(anon, token).get_json()["terms"], "7: HARD FAIL - a withdrawn guide's terms still hinted"
    rows = conn.execute("SELECT allowed FROM audit_log WHERE action = 'jarvis_api' AND target = 'guides_vocabulary'").fetchall()
    assert any(r["allowed"] == 1 for r in rows) and any(r["allowed"] == 0 for r in rows), "7: calls and refusals audited"
    audit = "\n".join(str(tuple(x)) for x in conn.execute("SELECT * FROM audit_log"))
    assert "B-PROG" not in audit and "KEEPME" not in audit, "7: the audit carries no term"
    plant(gconn, ids["ax200_v2"], 3, " ".join(f"ZQ-{i}" for i in range(120)))
    assert len(vocab(anon, token).get_json()["terms"]) <= 80, "7: at most 80 terms"
    page = admin.get("/jarvis/devices").text
    admin.post(f"/jarvis/devices/{device}/revoke", data={"csrf_token": csrf(page)})
    assert vocab(anon, token).status_code == 401, "7: HARD FAIL - a revoked device still gets the terms"
    conn.close()
    gconn.close()


def exchange_confirm():
    # 8. J-D10: the library's terms go to speech to text; what was heard waits on screen and nothing is asked until
    #    "Yes, ask this"; no, a timeout or anything ambiguous discards it unasked
    import time
    clock = Clock()
    pcm = (np.sin(np.arange(16000) / 5) * 3000).astype(np.int16)
    hints = []

    def stt_ok(pcm, hint=None):
        hints.append(hint)
        return "what does the B prog button do", "en"

    def fresh():
        m = states.Machine()
        m.go("READY", "listening for the wake phrase")
        m.go("ACTIVE", "heard the wake phrase - listening to the request")
        return m
    m, link, seen = fresh(), Link(ANSWER), {}

    def look_then_yes(card):
        seen["asked_before"] = list(link.asked)
        assert m.decide(card["id"], "ask"), "8: the confirmation is accepted"
    reason, card = run_exchange(answer.Exchange(m, stt_ok, lambda: link, clock=clock, confirm_seconds=3), pcm, m,
                                look_then_yes)
    assert hints == [["AX-200", "B-PROG", "DRY"]] and link.calls[0] == "vocabulary", \
        f"8: the library's terms are fetched first and handed to speech to text ({hints}, {link.calls})"
    assert card is not None and seen["asked_before"] == [], "8: HARD FAIL - the guides were asked before the confirmation"
    assert card["heard"] == "what does the B prog button do" and not card.get("citations"), "8: the exact transcript, alone"
    assert isinstance(card["id"], str) and len(card["id"]) >= 20, "8: an unguessable id for this one transcript"
    assert link.asked == ["what does the B prog button do"] and link.calls.count("ask") == 1, \
        f"8: once confirmed, exactly that text is asked once ({link.asked})"
    history = " ".join(h["reason"] for h in m.snapshot()["history"]) + " " + reason
    assert card["id"] not in history and "prog" not in history, "8: HARD FAIL - transcript or id in the state history"
    for label, decide, seconds in (("No, discard", lambda mm, c: mm.decide(c["id"], "discard"), 3),
                                   ("a timeout", None, 0.3),
                                   ("a wrong id", lambda mm, c: mm.decide("not-" + c["id"], "ask"), 3),
                                   ("an unknown decision", lambda mm, c: mm.decide(c["id"], "yes"), 3)):
        m, link = fresh(), Link(ANSWER)
        t0 = time.monotonic()
        reason, card = run_exchange(answer.Exchange(m, stt_ok, lambda: link, clock=clock, confirm_seconds=seconds), pcm, m,
                                    (lambda c, d=decide, mm=m: d(mm, c)) if decide else None)
        took = time.monotonic() - t0
        a = m.snapshot()["answer"]
        assert card is not None, f"8: setup - the transcript was shown ({label})"
        assert link.asked == [] and "ask" not in link.calls, f"8: HARD FAIL - {label} still asked the guides"
        assert a["outcome"] == "discarded" and not a.get("heard") and "Nothing was asked" in a["message"], \
            f"8: {label} discards what was heard ({a})"
        assert decide is None or took < 2, f"8: {label} ends the wait at once ({took:.1f} s)"
        assert m.decide(card["id"], "ask") is False and link.asked == [], f"8: HARD FAIL - a decision after {label} acted on"
        assert "prog" not in reason, "8: the reason carries no speech"
    # a second decision on one transcript is ambiguous; a new wake drops what was waiting
    m = fresh()
    pid = m.offer("what does the B prog button do", 3)
    assert m.decide(pid, "ask") is True and m.decide(pid, "ask") is False, "8: one decision per transcript"
    assert m.await_decision(pid, 0.1) == "ambiguous", "8: HARD FAIL - a second decision did not make it ambiguous"
    m = fresh()
    pid = m.offer("what does the B prog button do", 3)
    m.go("READY", "back to listening")
    m.go("ACTIVE", "heard the wake phrase - listening to the request")
    assert m.decide(pid, "ask") is False and m.await_decision(pid, 0.1) != "ask", "8: a new wake discards the old transcript"
    # no terms (no credential, link down, refused): nothing is transcribed and nothing asked
    for make in (lambda: Link(ANSWER, vocab_error=LinkDown("clinic app unreachable")),
                 lambda: Link(ANSWER, vocab_error=LinkRefused("device not registered or revoked")), lambda: None):
        m, hints[:] = fresh(), []
        lk = make()
        answer.Exchange(m, stt_ok, lambda: lk, clock=clock, confirm_seconds=3)(pcm)
        assert hints == [] and (lk is None or lk.asked == []), "8: without the library's terms nothing is transcribed"
        assert m.snapshot()["answer"]["outcome"] == "unavailable", "8: and the page says the guides cannot be reached"


def stt_hint():
    # 9. the hint travels to the speech-to-text child on stdin with the audio - never on its command line
    calls = []

    def run(cmd, input=None, capture_output=None, timeout=None, **kw):
        calls.append({"cmd": cmd, "input": input})
        out = {"text": "What does DRY do?", "language": "en", "segments": [{"no_speech_prob": 0.02, "avg_logprob": -0.2}]}
        return type("R", (), {"returncode": 0, "stdout": json.dumps(out).encode(), "stderr": b""})()
    pcm = (np.sin(np.arange(16000) / 5) * 3000).astype(np.int16)
    assert stt.transcribe(pcm, ["AX-300", "DRY"], run=run) == ("What does DRY do?", "en"), "9: the transcript comes back"
    got_pcm, got_hint = stt.read_request(calls[-1]["input"])
    assert got_hint == ["AX-300", "DRY"] and np.array_equal(got_pcm, pcm), "9: hint and audio both arrive intact"
    assert not any("AX-300" in str(a) or "DRY" in str(a) for a in calls[-1]["cmd"]), "9: the hint is not on the command line"
    assert stt.prompt_for([]) is None and stt.prompt_for(None) is None, "9: no terms, no prompt"
    p = stt.prompt_for(["AX-300", "DRY"])
    assert isinstance(p, str) and "AX-300" in p and "DRY" in p, f"9: the prompt carries the terms ({p!r})"
    # the child hands the prompt to Whisper (added after mutation run 1: N33 - the child ignoring it - survived)
    import contextlib
    import io
    import os
    import types

    class FakeModel:
        seen = {}

        def __init__(self, path, **kw):
            pass

        def detect_language(self, audio):
            return "en", 0.9, [("en", 0.9), ("it", 0.05)]

        def transcribe(self, audio, **kw):
            FakeModel.seen.update(kw, audio=audio)
            return iter([types.SimpleNamespace(text=" What does DRY do?", no_speech_prob=0.01, avg_logprob=-0.1)]), None
    saved = (sys.modules.get("faster_whisper"), sys.stdin, dict(os.environ))
    try:
        sys.modules["faster_whisper"] = types.SimpleNamespace(WhisperModel=FakeModel)
        for hint, want in ((["AX-300", "DRY"], stt.prompt_for(["AX-300", "DRY"])), ([], None)):
            FakeModel.seen.clear()
            sys.stdin = types.SimpleNamespace(buffer=io.BytesIO(stt.frame(pcm, hint)))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                stt.child()
            used = FakeModel.seen.get("initial_prompt") or FakeModel.seen.get("hotwords")
            assert used == want, f"9: the child gives Whisper the library's terms ({used!r}, wanted {want!r})"
            assert np.allclose(FakeModel.seen["audio"], pcm / 32768), "9: and the audio it was sent"
            assert json.loads(out.getvalue())["text"].strip() == "What does DRY do?", "9: one JSON line back"
    finally:
        if saved[0] is None:
            sys.modules.pop("faster_whisper", None)
        else:
            sys.modules["faster_whisper"] = saved[0]
        sys.stdin = saved[1]
        os.environ.clear()
        os.environ.update(saved[2])


def confirm_page():
    # 10. the confirmation on the Jarvis page: the exact transcript as text, two buttons only while it waits, and a
    #     decision taken only from this page's own origin, as JSON, for the id on screen
    m = states.Machine()
    m.go("READY", "listening")
    m.go("ACTIVE", "heard the wake phrase - listening to the request")
    pid = m.offer("<b>what</b> does the B prog button do", 30)
    c = jarvis_app(m).test_client()
    base = "http://127.0.0.1:5020"
    html = c.get("/", base_url=base).data.decode()
    assert "<b>what</b>" not in html and "&lt;b&gt;what&lt;/b&gt;" in html, "10: HARD FAIL - the transcript rendered as markup"
    assert "Did I hear you right?" in html and "Yes, ask this" in html and "No, discard" in html, "10: the question and choices"
    assert html.lower().count("<button") == 2, "10: the confirmation's two buttons are the only ones"
    assert json.loads(c.get("/status", base_url=base).data)["answer"]["id"] == pid, "10: the page can read the id"

    def post(body, origin=base, ctype="application/json", at=base):
        return c.post("/confirm", data=body, content_type=ctype, headers={"Origin": origin} if origin else {}, base_url=at)
    good = json.dumps({"id": pid, "decision": "ask"})
    for label, r in (("a foreign origin", post(good, origin="http://evil.example")),
                     ("no origin", post(good, origin=None)),
                     ("a form post", post(f"id={pid}&decision=ask", ctype="application/x-www-form-urlencoded")),
                     ("text/plain", post(good, ctype="text/plain")),
                     ("a foreign host", post(good, at="http://evil.example:5020"))):
        assert r.status_code in (400, 403, 415), f"10: HARD FAIL - {label} accepted ({r.status_code})"
    r = post(good)
    assert r.status_code == 204, f"10: the page's own request is taken ({r.status_code})"
    assert m.await_decision(pid, 0.1) == "ask", "10: HARD FAIL - a refused request had already registered a decision"
    assert post(json.dumps({"id": pid, "decision": "ask"})).status_code == 409, "10: nothing waits any more"
    # "No, discard" from the page is what is acted on (added after mutation run 1: N28 - every choice taken as yes - survived)
    pid = m.offer("what does the B prog button do", 30)
    assert post(json.dumps({"id": pid, "decision": "discard"})).status_code == 204, "10: the page's no is taken"
    assert m.await_decision(pid, 0.1) == "discard", "10: HARD FAIL - the page's no was not what the exchange saw"


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        route(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        spoken_codes(Path(tmp))
    exchange()
    listener_hands_over()
    stt_child()
    page()
    with tempfile.TemporaryDirectory() as tmp:
        vocabulary_route(Path(tmp))
    exchange_confirm()
    stt_hint()
    confirm_page()
    print("jarvis_guides_selftest: ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"] or not sys.argv[1:]:
        selftest()
    sys.exit(0)
