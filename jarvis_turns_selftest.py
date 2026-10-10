"""Jarvis J02 follow-up 6 (R1, R2): listening goes on during an exchange, a new wake or a lost microphone ends the
interaction in progress, nothing from a cancelled interaction is shown or asked, deadlines count sleep, and the page says
what is really happening (a lost connection, which kind of failure, what became of a click).

A fake microphone, detector, clock, speech to text and clinic link; a real listener thread, real exchange threads and a
real speech-to-text child process only where a child is the point (check 7). Expectations fixed in
.planning/plans/JARVIS.md §18 before the code.
"""
import json
import queue
import subprocess
import sys
import threading
import time

import numpy as np

from jarvis import answer, listen, states, stt
from jarvis.clinic import LinkDown, LinkRefused
from jarvis.web import create_app
import jarvis_indicator

CHUNK = 1280
SOUND = (np.sin(np.arange(CHUNK) / 7) * 2000).astype(np.int16)
SPEECH = (np.sin(np.arange(CHUNK) / 3) * 6000).astype(np.int16)
WAKE = (np.sin(np.arange(CHUNK) / 5) * 1500).astype(np.int16)
ZERO = np.zeros(CHUNK, np.int16)
BASE = "http://127.0.0.1:5020"
ANSWER = {"asked_as": "What does the B-PROG button do?", "outcome": "answer", "reason": None, "message": "",
          "escalation": "", "citations": [{"title": "DemoMed AX-200 User manual", "edition": "2", "page": 2,
                                           "passage": "B-PROG selects program B.", "verified": True}], "warnings": []}


def until(pred, seconds=3.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


class Clock:
    """The listener's clocks (monotonic, wall) and the machine's sleep-counting clock, moved by hand."""

    def __init__(self):
        self.mono, self.wall, self.awake = 1000.0, 1_800_000_000.0, 5000.0

    def tick(self, seconds=0.08):
        self.mono += seconds
        self.wall += seconds
        self.awake += seconds

    def sleep(self, seconds):
        # the Mac sleeps: the wall and the sleep-counting clock move on, the monotonic clock does not
        self.wall += seconds
        self.awake += seconds


class Feed:
    """A microphone the test speaks into, one chunk at a time; 'error' = the device fails."""

    def __init__(self, clock):
        self.q, self.clock, self.read_count = queue.Queue(), clock, 0

    def open(self):
        pass

    def read(self, timeout):
        try:
            item = self.q.get(timeout=timeout)
        except queue.Empty:
            return None
        if isinstance(item, str) and item == "stop":
            raise StopIteration
        if isinstance(item, str) and item == "error":
            raise listen.MicError("the microphone stopped")
        self.clock.tick()
        self.read_count += 1
        return item

    def put(self, *chunks):
        for c in chunks:
            self.q.put(c)

    def drained(self):
        return self.q.empty()

    def close(self):
        pass


class Detector:
    def feed(self, chunk):
        return np.array_equal(chunk, WAKE)

    def reset(self):
        pass


class Service:
    """A listener in its own thread, as jarvis_run runs it, with `on_request` as the exchange."""

    def __init__(self, on_request=None, machine=None):
        self.clock = Clock()
        self.m = machine or states.Machine(clock=lambda: self.clock.awake)
        self.feed = Feed(self.clock)
        self.lst = listen.Listener(self.m, Detector, lambda: self.feed, mono=lambda: self.clock.mono,
                                   wall=lambda: self.clock.wall, on_request=on_request)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.lst.run, args=(self.stop,), daemon=True)
        self.thread.start()

    def ready(self):
        self.feed.put(*[SOUND] * 15)
        assert until(lambda: self.m.state == "READY"), f"setup: READY on real sound ({self.m.state})"

    def ask(self):
        """Wake phrase, a request, then silence: the request is handed over."""
        self.feed.put(WAKE, *[SPEECH] * 8, *[SOUND] * 17)

    def wakes(self):
        return sum(1 for h in self.m.snapshot()["history"] if h["reason"].startswith("heard the wake phrase"))

    def close(self):
        self.stop.set()
        self.feed.put("stop")
        self.thread.join(3)


class Blocking:
    """An exchange that holds until released - long enough to say something else meanwhile."""

    def __init__(self, m):
        self.m, self.started, self.release, self.turns = m, threading.Event(), threading.Event(), []

    def __call__(self, pcm, *turn):
        self.turns.append(turn[0] if turn else None)
        self.started.set()
        self.release.wait(10)
        if turn:
            self.m.show_for(turn[0], {**ANSWER, "heard": "first question"}, 120)
        return "answered on screen (DemoMed AX-200 User manual, page 2)"


def listening_during_an_exchange():
    # 1. the listener keeps reading while an exchange runs: a new wake is heard and starts a new interaction, and what
    #    the first one finishes later is never shown (§17.2 divergence 1)
    svc = Service()
    ex = Blocking(svc.m)
    svc.lst.on_request = ex
    try:
        svc.ready()
        svc.ask()
        assert ex.started.wait(3), "setup: the first request reached the exchange"
        svc.feed.put(*[SOUND] * 5, WAKE)
        heard = until(lambda: svc.wakes() == 2, 2.0)
        assert heard, "1: HARD FAIL - a new wake phrase during an exchange was not heard (the listener waits for it)"
        assert svc.m.state == "ACTIVE", f"1: the new wake starts a new interaction ({svc.m.state})"
        ex.release.set()
        time.sleep(0.2)
        a = svc.m.snapshot()["answer"]
        assert not (a and a.get("heard") == "first question"), "1: HARD FAIL - a cancelled interaction's answer was shown"
        assert svc.m.state == "ACTIVE", f"1: the old exchange's end does not end the new one ({svc.m.state})"
    finally:
        ex.release.set()
        svc.close()


def device_faults_during_an_exchange():
    # 2. a microphone fault, a muted microphone or a sleep during an exchange gives DEGRADED within 5 s and cancels it
    for label, fault in (("the device failing", ["error"]), ("a muted microphone", [ZERO] * 30), ("a sleep", "sleep")):
        svc = Service()
        ex = Blocking(svc.m)
        svc.lst.on_request = ex
        try:
            svc.ready()
            svc.ask()
            assert ex.started.wait(3), f"setup ({label}): the request reached the exchange"
            t0 = time.monotonic()
            if fault == "sleep":
                svc.clock.sleep(600)
                svc.feed.put(SOUND)
            else:
                svc.feed.put(*fault)
            ok = until(lambda: svc.m.state == "DEGRADED", 5.0)
            assert ok, f"2: HARD FAIL - {label} during an exchange was not noticed ({svc.m.state})"
            assert time.monotonic() - t0 <= 5.0, f"2: {label} noticed within 5 s"
            ex.release.set()
            time.sleep(0.2)
            a = svc.m.snapshot()["answer"]
            assert a is None or a.get("outcome") == "cancelled", f"2: {label}: nothing of the cancelled exchange shown ({a})"
            assert svc.m.state in ("DEGRADED", "STARTING", "READY"), f"2: {label}: the old exchange cannot end in READY"
        finally:
            ex.release.set()
            svc.close()


class Link:
    """The clinic link: each call can be held until released; whoami can change underneath."""

    def __init__(self, result=ANSWER, hold_ask=False):
        self.result, self.asked, self.calls = result, [], []
        self.ask_started, self.ask_release = threading.Event(), threading.Event()
        self.hold_ask = hold_ask
        self.who = {"device": "Reception Mac", "device_id": 1, "delegation": None}
        self.who_error = None

    def whoami(self):
        self.calls.append("whoami")
        if self.who_error:
            raise self.who_error
        return dict(self.who)

    def vocabulary(self):
        self.calls.append("vocabulary")
        return ["AX-200", "B-PROG"]

    def ask_guides(self, question):
        self.calls.append("ask")
        self.asked.append(question)
        self.ask_started.set()
        if self.hold_ask:
            self.ask_release.wait(10)
        return self.result


class Stt:
    def __init__(self, hold=False, text="what does the B prog button do", error=None):
        self.hold, self.text, self.error = hold, text, error
        self.started, self.release, self.cancelled_seen = threading.Event(), threading.Event(), []

    def __call__(self, pcm, hint=None, cancelled=None):
        self.started.set()
        if self.hold:
            while not self.release.wait(0.02):
                if cancelled and cancelled():
                    self.cancelled_seen.append(True)
                    raise stt.Cancelled()
        if self.error:
            raise self.error
        return self.text, "en"


def machine(clock=None):
    m = states.Machine(clock=clock or (lambda: time.monotonic()))
    m.slice = 0.02
    m.go("READY", "listening for the wake phrase")
    return m


def worker(ex, pcm, turn, m):
    t = threading.Thread(target=lambda: m.finish(turn, ex(pcm, turn)), daemon=True)
    t.start()
    return t


PCM = (np.sin(np.arange(16000) / 5) * 3000).astype(np.int16)


def cancelled_work_is_never_shown():
    # 3. a new wake during speech to text, the confirmation or the guide call: the old interaction ends unasked (or, once
    #    asked, unshown); the speech-to-text work is told to stop; only one ask per confirmed question
    # during speech to text
    m, link, s = machine(), Link(), Stt(hold=True)
    turn = m.begin("heard the wake phrase - listening to the request")
    t = worker(answer.Exchange(m, s, lambda: link, recheck_seconds=0.05), PCM, turn, m)
    assert s.started.wait(3), "setup: speech to text started"
    new = m.begin("heard the wake phrase - listening to the request")
    t.join(3)
    assert not t.is_alive() and s.cancelled_seen, "3: HARD FAIL - speech to text of a cancelled interaction was not stopped"
    assert link.asked == [] and m.turn == new and m.state == "ACTIVE", "3: nothing asked; the new interaction goes on"
    a = m.snapshot()["answer"]
    assert a and a["outcome"] == "cancelled" and "Nothing was asked" in a["message"], f"3: said so on screen ({a})"
    # during the confirmation
    m, link = machine(), Link()
    turn = m.begin("heard the wake phrase - listening to the request")
    t = worker(answer.Exchange(m, Stt(), lambda: link, recheck_seconds=0.05), PCM, turn, m)
    assert until(lambda: m.state == "WAITING_FOR_CONFIRMATION"), f"3: waiting is its own state ({m.state})"
    card = m.snapshot()["answer"]
    assert card["outcome"] == "confirm", "setup: the card"
    m.begin("heard the wake phrase - listening to the request")
    assert m.decide(card["id"], "ask") == "gone", "3: HARD FAIL - the old card could still be confirmed after a new wake"
    t.join(3)
    assert not t.is_alive() and link.asked == [], "3: HARD FAIL - the cancelled question was asked"
    # during the guide call
    m, link = machine(), Link(hold_ask=True)
    turn = m.begin("heard the wake phrase - listening to the request")
    t = worker(answer.Exchange(m, Stt(), lambda: link, recheck_seconds=0.05), PCM, turn, m)
    assert until(lambda: (m.snapshot()["answer"] or {}).get("outcome") == "confirm"), "setup: the card"
    assert m.decide(m.snapshot()["answer"]["id"], "ask") == "taken", "setup: confirmed"
    assert link.ask_started.wait(3), "setup: the guides are being asked"
    new = m.begin("heard the wake phrase - listening to the request")
    link.ask_release.set()
    t.join(3)
    a = m.snapshot()["answer"]
    assert link.asked == ["what does the B prog button do"], f"3: asked once ({link.asked})"
    assert a and a["outcome"] == "cancelled" and not a.get("citations"), f"3: HARD FAIL - the late answer was shown ({a})"
    assert "not shown" in a["message"], f"3: the card says the answer is not shown ({a['message']})"
    assert m.turn == new and m.state == "ACTIVE", "3: the new interaction is untouched"
    # the one moment a question may go to the guides comes once per interaction, and never for an old one
    assert m.sending(new) is True and m.sending(new) is False, "3: HARD FAIL - an interaction could send twice"
    assert m.sending(turn) is False, "3: HARD FAIL - a cancelled interaction could still send"


def deadlines_count_sleep():
    # 4. the confirmation window counts the time the Mac slept; a click after it never revives the question; the answer
    #    goes away by the same clock
    assert states.awake_clock() > 0 and abs(states.awake_clock() - time.clock_gettime(time.CLOCK_MONOTONIC_RAW)) < 1, \
        "4: deadlines read CLOCK_MONOTONIC_RAW, which counts sleep on macOS (time.monotonic does not)"
    now = [100.0]
    m, link = machine(clock=lambda: now[0]), Link()
    turn = m.begin("heard the wake phrase - listening to the request")
    t = worker(answer.Exchange(m, Stt(), lambda: link, confirm_seconds=30, recheck_seconds=0.05), PCM, turn, m)
    assert until(lambda: (m.snapshot()["answer"] or {}).get("outcome") == "confirm"), "setup: the card"
    card = m.snapshot()["answer"]
    now[0] += 45                                         # slept through the window
    assert m.decide(card["id"], "ask") == "expired", "4: HARD FAIL - a click after the deadline was taken"
    t.join(3)
    assert not t.is_alive() and link.asked == [], "4: HARD FAIL - an expired question was asked"
    a = m.snapshot()["answer"]
    assert a["outcome"] == "discarded" and "30 seconds" in a["message"], f"4: says it expired ({a})"
    m.show({"outcome": "answer", "heard": "x", "citations": [], "warnings": []}, 120)
    now[0] += 121
    assert m.snapshot()["answer"] is None, "4: the answer's two minutes count sleep too"


def revocation_and_delegation():
    # 5. a device refused or a delegation that ends or changes while the card waits cancels it within the recheck time
    for label, change in (("the device revoked", lambda lk: setattr(lk, "who_error", LinkRefused("device not registered or revoked"))),
                          ("the delegation ended", lambda lk: lk.who.update(delegation=None)),
                          ("another person's delegation", lambda lk: lk.who.update(delegation={"username": "b", "role": "assistant",
                                                                                                 "expires_at": "x"}))):
        m, link = machine(), Link()
        link.who["delegation"] = {"username": "a", "role": "dentist", "expires_at": "x"}
        turn = m.begin("heard the wake phrase - listening to the request")
        t = worker(answer.Exchange(m, Stt(), lambda: link, recheck_seconds=0.05), PCM, turn, m)
        assert until(lambda: m.state == "WAITING_FOR_CONFIRMATION"), f"setup ({label})"
        card = m.snapshot()["answer"]
        t0 = time.monotonic()
        change(link)
        assert until(lambda: m.snapshot()["answer"]["outcome"] != "confirm", 2), f"5: HARD FAIL - {label} did not cancel"
        assert time.monotonic() - t0 < 1, f"5: {label} noticed at the next recheck"
        assert m.decide(card["id"], "ask") == "gone" and link.asked == [], f"5: HARD FAIL - asked after {label}"
        t.join(3)
    # the running service rechecks every RECHECK_SECONDS; with the loopback call it must stay within 5 s (the injected
    # run on the running service measured 5.08 s with 5 s - JARVIS §18)
    assert answer.RECHECK_SECONDS <= 4, f"5: recheck every {answer.RECHECK_SECONDS} s cannot notice within 5 s"
    # an old interaction's recheck never cancels a newer one (found in development: cancel() once took no number)
    m, link = machine(), Link()
    old = m.begin("heard the wake phrase - listening to the request")
    new = m.begin("heard the wake phrase - listening to the request")
    link.who_error = LinkRefused("device not registered or revoked")
    try:
        answer.Exchange(m, Stt(), lambda: link).recheck(old, link, answer.identity(Link().who))
    except answer.Stop:
        pass
    assert m.turn == new and m.state == "ACTIVE", "5: HARD FAIL - an old interaction's recheck cancelled the new one"


def states_and_indicator():
    # 6. WAITING_FOR_CONFIRMATION is a state of its own: reached only from ACTIVE, left to ACTIVE, READY or DEGRADED;
    #    its meaning says nothing is recorded; the indicator names it
    assert "WAITING_FOR_CONFIRMATION" in states.STATES
    assert states.allowed("ACTIVE", "WAITING_FOR_CONFIRMATION") and not states.allowed("READY", "WAITING_FOR_CONFIRMATION")
    for b in ("ACTIVE", "READY", "DEGRADED"):
        assert states.allowed("WAITING_FOR_CONFIRMATION", b), b
    meaning = states.MEANING["WAITING_FOR_CONFIRMATION"].lower()
    assert "not recording" in meaning and "confirm" in meaning, meaning
    assert "WAITING_FOR_CONFIRMATION" in jarvis_indicator.label({"state": "WAITING_FOR_CONFIRMATION"})
    assert set(jarvis_indicator.MARK) == set(states.STATES), "6: every state has its own mark in the menu bar"


def stt_child_is_stopped():
    # 7. a real child: cancelled -> killed within a second, nothing left running; too slow -> a speech-to-text failure,
    #    not "did not catch that"; a crash -> a failure; low confidence -> unclear
    hold = [sys.executable, "-c", "import sys, time; sys.stdin.buffer.read(); time.sleep(30)"]
    flag = threading.Event()
    threading.Timer(0.3, flag.set).start()
    t0 = time.monotonic()
    try:
        stt.run_child(hold, b"x" * 200000, 3, flag.is_set)
    except stt.Cancelled:
        pass
    except Exception as e:
        raise AssertionError(f"7: HARD FAIL - a cancelled child was not stopped as cancelled ({type(e).__name__})")
    else:
        raise AssertionError("7: a cancelled child was not reported as cancelled")
    assert time.monotonic() - t0 < 1.5, f"7: cancelled promptly ({time.monotonic() - t0:.2f} s)"
    left = subprocess.run(["pgrep", "-f", "time.sleep\\(30\\)"], capture_output=True, text=True).stdout.split()
    assert not left, f"7: HARD FAIL - the cancelled child is still running ({left})"
    try:
        stt.run_child(hold, b"", 0.3, lambda: False)
    except subprocess.TimeoutExpired:
        pass
    else:
        raise AssertionError("7: a child over its time was not stopped")
    out = stt.run_child([sys.executable, "-c", "import sys; print(len(sys.stdin.buffer.read()))"], b"x" * 300000, 10,
                        lambda: False)
    assert out.returncode == 0 and out.stdout.strip() == b"300000", "7: a normal child gets all its input and returns"
    # a child that reads its input late, as the real one does after importing Whisper: more than a pipe buffer must
    # still arrive (found in development: retrying communicate() never resumes writing stdin - every real
    # transcription timed out at 20 s while this check, without the delay, passed)
    t0 = time.monotonic()
    out = stt.run_child([sys.executable, "-c", "import sys, time; time.sleep(0.5); print(len(sys.stdin.buffer.read()))"],
                        b"x" * 300000, 5, lambda: False)
    assert out.returncode == 0 and out.stdout.strip() == b"300000" and time.monotonic() - t0 < 3, \
        f"7: HARD FAIL - a child that reads late did not get its input ({time.monotonic() - t0:.1f} s)"

    def fake(code=0, out=None, raises=None):
        def run(cmd, input=None, capture_output=None, timeout=None, **kw):
            if raises:
                raise raises
            body = json.dumps(out).encode() if out else b""
            return type("R", (), {"returncode": code, "stdout": body, "stderr": b""})()
        return run
    for label, run, want in (("a crash", fake(code=1), stt.Failed),
                             ("too slow", fake(raises=subprocess.TimeoutExpired("x", 20)), stt.Failed),
                             ("low confidence", fake(out={"text": "blah", "language": "en",
                                                          "segments": [{"no_speech_prob": 0.1, "avg_logprob": -1.5}]}),
                              stt.Unclear)):
        try:
            stt.transcribe(PCM, run=run)
        except want:
            pass
        except Exception as e:
            raise AssertionError(f"7: {label} gave {type(e).__name__}, wanted {want.__name__}")
        else:
            raise AssertionError(f"7: {label} was accepted")
    assert not issubclass(stt.Failed, stt.Unclear) and not issubclass(stt.Unclear, stt.Failed), "7: two kinds, apart"


def failures_told_apart():
    # 8. unclear speech, a speech-to-text fault, the clinic app down, no setup and a refused device are five different
    #    cards, each saying nothing was asked; switched off is a sixth and transcribes nothing
    cases = (("unclear", lambda: Link(), Stt(error=stt.Unclear("not sure what was said"))),
             ("speech_to_text", lambda: Link(), Stt(error=stt.Failed("speech to text is not available on this machine"))),
             ("clinic_down", lambda: type("L", (Link,), {"vocabulary": lambda self: (_ for _ in ()).throw(
                 LinkDown("clinic app unreachable"))})(), Stt()),
             ("not_set_up", lambda: None, Stt()),
             ("refused", lambda: type("L", (Link,), {"vocabulary": lambda self: (_ for _ in ()).throw(
                 LinkRefused("device not registered or revoked"))})(), Stt()))
    titles = set()
    for problem, make, s in cases:
        m = machine()
        lk = make()
        turn = m.begin("heard the wake phrase - listening to the request")
        reason = answer.Exchange(m, s, lambda: lk, recheck_seconds=0.05)(PCM, turn)
        a = m.snapshot()["answer"]
        assert a["problem"] == problem, f"8: {problem} told apart ({a.get('problem')}: {a.get('message')})"
        assert "Nothing was asked" in a["message"] and a.get("title"), f"8: {problem}: a title and nothing asked ({a})"
        assert problem == "unclear" or "did not catch" not in a["message"].lower(), \
            f"8: {problem} is not worded as a hearing problem ({a['message']})"
        assert "what does" not in reason, "8: no speech in the reason"
        titles.add(a["title"])
    assert len(titles) == len(cases), f"8: five different titles ({titles})"
    m, s = machine(), Stt()
    turn = m.begin("heard the wake phrase - listening to the request")
    answer.Exchange(m, s, lambda: Link(), enabled=False)(PCM, turn)
    a = m.snapshot()["answer"]
    assert (a or {}).get("problem") == "switched_off" and not s.started.is_set(), \
        "8: HARD FAIL - switched off still transcribed"


def page_says_what_is_true():
    # 9. the page: a heartbeat it can miss, a lost-connection state, a status region for the click's result, the answers
    #    line apart from listening; /confirm says why a click was refused
    m = machine()
    app = create_app(m)
    c = app.test_client()
    html = c.get("/", base_url=BASE).data.decode()
    code = _code_only(html)
    assert "es.onerror" in code and "not reachable" in html.lower(), "9: a lost stream is shown, not the last state"
    assert "STALE_MS" in code and "setInterval" in code, "9: a stalled stream is noticed by a watchdog too"
    # back after a freeze the stream sends only heartbeats, which carry no state: the page reads it again (added after
    # the Chromium page check run 1 left NOT REACHABLE on screen after SIGCONT)
    assert "/status" in _fn_body(code, "seen") and "render" in _fn_body(code, "seen"), \
        "9: on coming back the page reads the whole state again"
    assert 'role="status"' in html and 'id="confirm-status"' in html, "9: the click's result is announced"
    body = _fn_body(code, "decide")
    assert ".then(" in body and ".catch(" in body, "9: the page reads the answer to its click"
    m.set_answers(False, "switched off on this computer")
    html = c.get("/", base_url=BASE).data.decode()
    assert "Clinic-guide answers:" in html and "switched off on this computer" in html, "9: answers shown apart"
    assert "Listening:" in html, "9: listening readiness shown on its own line"
    resp = c.get("/events", base_url=BASE)
    stream = iter(resp.response)
    first = next(stream)
    first = first.decode() if isinstance(first, bytes) else first
    assert first.startswith("data: "), first[:40]
    t0 = time.monotonic()
    second = next(stream)
    second = second.decode() if isinstance(second, bytes) else second
    assert second.startswith("event: ping") and time.monotonic() - t0 < 3.5, \
        f"9: a heartbeat the page can see, every few seconds ({second[:30]!r})"
    resp.close()
    turn = m.begin("heard the wake phrase - listening to the request")
    m.go_for(turn, "ACTIVE", "working out what was asked (on this computer)")
    pid = m.offer(turn, "what does the B prog button do", 30)

    def post(body):
        return c.post("/confirm", data=json.dumps(body), content_type="application/json", headers={"Origin": BASE},
                      base_url=BASE)
    r = post({"id": "nope-" + pid, "decision": "ask"})
    assert r.status_code == 409 and (r.get_json(silent=True) or {}).get("result") == "gone", \
        f"9: a stale card is refused with its reason ({r.data})"
    assert snapshot_state(m) == "WAITING_FOR_CONFIRMATION"


def snapshot_state(m):
    return m.snapshot()["state"]


def _code_only(html):
    """The page's script without comments (a comment must not satisfy a check)."""
    import re
    script = html[html.index("<script>"):html.rindex("</script>")]
    script = re.sub(r"/\*.*?\*/", "", script, flags=re.S)
    return re.sub(r"(?m)//.*$", "", script)


def _fn_body(code, name):
    start = code.index(f"function {name}(")
    depth, i = 0, code.index("{", start)
    for j in range(i, len(code)):
        depth += {"{": 1, "}": -1}.get(code[j], 0)
        if depth == 0:
            return code[i:j + 1]
    raise AssertionError(f"no body for {name}")


def exchange_fault(faults):
    # 12. an exchange that fails unexpectedly ends its interaction (back to READY, logged) - it never leaves the state
    #     stuck or stops the listening
    svc = Service()

    def broken(pcm, turn):
        raise RuntimeError("boom")
    svc.lst.on_request = broken
    try:
        svc.ready()
        svc.ask()
        assert until(lambda: svc.m.state == "READY" and "the exchange failed" in svc.m.reason, 3), \
            f"12: HARD FAIL - a failed exchange left {svc.m.state} ({svc.m.reason})"
        assert sum("exchange fault" in f for f in faults) == 1, f"12: the fault is logged once ({faults})"
        faults[:] = [f for f in faults if "exchange fault" not in f]
        svc.lst.on_request = lambda pcm, turn: "answered"
        svc.ask()
        assert until(lambda: svc.wakes() == 2), "12: and the next wake is heard"
    finally:
        svc.close()


def resources_are_released():
    # 10. repeated wakes do not pile up workers: every cancelled exchange thread ends, the listener's own included
    svc = Service()
    ex = Blocking(svc.m)
    svc.lst.on_request = ex
    try:
        svc.ready()
        for _ in range(4):
            ex.started.clear()
            svc.ask()
            assert ex.started.wait(3), "setup: a request reached the exchange"
        ex.release.set()
        assert until(lambda: not [t for t in threading.enumerate() if t.name.startswith("jarvis-exchange")], 3), \
            "10: exchange threads left running"
        assert len(set(ex.turns)) == 4, f"10: four interactions, each its own number ({ex.turns})"
    finally:
        ex.release.set()
        svc.close()


def answers_switch():
    # 11. clinic-guide answers are off unless the service is started for the synthetic demo; a credential alone never
    #     turns them on; the answers line says why they cannot be given, apart from listening
    import jarvis_run
    from jarvis import runtime
    assert jarvis_run.answers_on({"JARVIS_GUIDE_ANSWERS": "demo"}), "11: demo switches answers on"
    for env in ({}, {"JARVIS_GUIDE_ANSWERS": "1"}, {"JARVIS_GUIDE_ANSWERS": "on"}, {"JARVIS_GUIDE_ANSWERS": "DEMO "}):
        assert not jarvis_run.answers_on(env), f"11: HARD FAIL - {env} switched answers on"
    import inspect
    plist = inspect.getsource(__import__("jarvis.launchd", fromlist=["plist"]))
    assert "JARVIS_GUIDE_ANSWERS" not in plist, "11: HARD FAIL - the login item switches answers on"

    class Ok:
        def whoami(self):
            return {"device": "Reception Mac", "device_id": 1, "delegation": None}

    class Refused:
        def whoami(self):
            raise LinkRefused("device not registered or revoked")
    for on, link, want_on, words in ((False, Ok(), False, "switched off"), (True, None, False, "no device credential"),
                                     (True, Refused(), False, "refused this computer"), (True, Ok(), True, "SYNTHETIC DEMO")):
        m = machine()
        runtime.check_link(m, link, on)
        a = m.snapshot()["answers"]
        assert a["on"] is want_on and words in a["detail"], f"11: answers line ({on}, {a})"
        assert m.state == "READY", "11: the answers line never changes the listening state"


def selftest():
    # an exception in a worker thread, or an exchange fault the listener logged, fails the suite (added after a
    # KeyError in a cancelled exchange's thread went unnoticed by every assertion during development)
    import logging
    faults = []
    threading.excepthook = lambda a: faults.append(f"{a.thread.name}: {a.exc_type.__name__}: {a.exc_value}")
    handler = logging.Handler()
    handler.emit = lambda record: faults.append(record.getMessage()) if record.levelno >= logging.ERROR else None
    logging.getLogger().addHandler(handler)
    states_and_indicator()
    listening_during_an_exchange()
    device_faults_during_an_exchange()
    cancelled_work_is_never_shown()
    deadlines_count_sleep()
    revocation_and_delegation()
    stt_child_is_stopped()
    failures_told_apart()
    page_says_what_is_true()
    resources_are_released()
    answers_switch()
    exchange_fault(faults)
    time.sleep(0.3)
    assert not faults, f"HARD FAIL - a fault in a background thread: {faults}"
    print("jarvis_turns_selftest: ok")


if __name__ == "__main__":
    if sys.argv[1:] in ([], ["--selftest"]):
        selftest()
    sys.exit(0)
