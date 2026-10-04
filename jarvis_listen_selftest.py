"""Jarvis J01: always-ready listening - READY only on real audio, wake -> ACTIVE -> READY, DEGRADED and recovery,
privacy of idle audio, the clinic link shown apart from listening, the indicator's labels, the login items.

A fake microphone, detector, cue and clock; no hardware, no model, no network. Expectations fixed in
.planning/plans/JARVIS.md §9 before the code.
"""
import builtins
import socket
import sys
import tempfile
from pathlib import Path

import numpy as np

from jarvis import launchd, listen, runtime, states
from jarvis.clinic import LinkDown
import jarvis_indicator

CHUNK = 1280
SOUND = (np.sin(np.arange(CHUNK) / 7) * 2000).astype(np.int16)
SPEECH = (np.sin(np.arange(CHUNK) / 3) * 6000).astype(np.int16)
ZERO = np.zeros(CHUNK, np.int16)


class Clock:
    def __init__(self):
        self.mono, self.wall = 1000.0, 1_800_000_000.0

    def tick(self, seconds=0.08, sleep=0.0):
        self.mono += seconds
        self.wall += seconds + sleep


class Source:
    """Plays a script of chunks; None = no audio arrived; 'error' = the device failed."""

    def __init__(self, script, fail_open=None):
        self.script, self.fail_open, self.opened = list(script), fail_open, 0

    def open(self):
        if self.fail_open:
            raise listen.MicError(self.fail_open)
        self.opened += 1

    def read(self, timeout):
        if not self.script:
            raise StopIteration
        item = self.script.pop(0)
        if isinstance(item, str):
            raise listen.MicError("the microphone stopped")
        return item

    def close(self):
        pass


class Detector:
    def __init__(self, wake_at=()):
        self.wake_at, self.n = set(wake_at), 0

    def feed(self, chunk):
        self.n += 1
        return self.n in self.wake_at

    def reset(self):
        pass


def run(script, detector=None, engine_error=None, fail_open=None, sleep_at=None):
    m = states.Machine()
    clock = Clock()
    cues = []

    def make_detector():
        if engine_error:
            raise listen.EngineMissing(engine_error)
        return detector or Detector()
    src = Source(script, fail_open)
    lst = listen.Listener(m, make_detector, lambda: src, cue=lambda: cues.append(clock.mono),
                          mono=lambda: clock.mono, wall=lambda: clock.wall)
    seen = []
    lst.start()
    i = 0
    while True:
        try:
            lst.step()
        except StopIteration:
            break
        i += 1
        seen.append(m.state)
        clock.tick(sleep=600 if sleep_at == i else 0.0)
        if i > 5000:
            break
    return m, lst, seen, cues


def ready_only_on_real_audio():
    # 1. no engine, no microphone, only zeros: never READY, DEGRADED with the reason
    m, _l, seen, _c = run([SOUND] * 50, engine_error="no wake model")
    assert "READY" not in seen and m.state == "DEGRADED" and "wake engine" in m.reason, (m.state, m.reason)
    m, _l, seen, _c = run([SOUND] * 50, fail_open="no microphone available")
    assert "READY" not in seen and m.state == "DEGRADED" and "microphone" in m.reason, (m.state, m.reason)
    m, _l, seen, _c = run([ZERO] * 60)
    assert "READY" not in seen and m.state == "DEGRADED" and "muted or access denied" in m.reason, \
        f"1: zeros from macOS mean denied or muted ({m.state}: {m.reason})"
    # 2. real sound: READY only after a second of it has been processed
    m, _l, seen, _c = run([SOUND] * 30)
    first = seen.index("READY")
    assert first >= 12 and m.state == "READY", f"2: READY only after real audio ({first})"
    # 3. denied then granted: recovery through STARTING
    m, _l, seen, _c = run([ZERO] * 40 + [SOUND] * 30)
    assert m.state == "READY" and [h["state"] for h in m.snapshot()["history"]][-3:] == ["DEGRADED", "STARTING", "READY"], \
        "3: recovery when the microphone returns"


def wake_and_return():
    # 4. wake phrase -> ACTIVE with one cue; speech then silence -> READY within the thresholds
    script = [SOUND] * 20 + [SPEECH] * 20 + [SOUND] * 40
    m, lst, seen, cues = run(script, detector=Detector(wake_at={20}))
    assert "ACTIVE" in seen and len(cues) == 1, f"4: one cue at the wake ({len(cues)})"
    a = seen.index("ACTIVE")
    assert "READY" in seen[a:], "4: ACTIVE must end in READY after the request"
    back = seen.index("READY", a)
    speech_end = 40
    assert (back - speech_end) * 0.08 <= listen.SILENCE_SECONDS + 0.2, "4: back to READY shortly after speech stops"
    # no request after the wake: back to READY after the idle timeout, never later than 8 s
    m, lst, seen, cues = run([SOUND] * 20 + [SOUND] * 150, detector=Detector(wake_at={20}))
    a = seen.index("ACTIVE")
    assert "READY" in seen[a:], "4: an empty wake must return to READY"
    back = seen.index("READY", a)
    assert (back - a) * 0.08 <= 8.0, f"4: an empty wake returns within 8 s ({(back - a) * 0.08:.1f})"
    # 5. the idle buffer is at most 2 s and is emptied on the wake and on the return
    m, lst, seen, cues = run([SOUND] * 100)
    assert len(lst.buffer) <= int(2.0 / 0.08), f"5: idle buffer bounded ({len(lst.buffer)})"
    m, lst, seen, cues = run([SOUND] * 20 + [SPEECH] * 10 + [SOUND] * 40, detector=Detector(wake_at={20}))
    assert lst.heard_bytes == 0 and len(lst.buffer) <= 25, "5: the request audio is discarded after the exchange"


def degraded_and_recovery():
    # 6. a sleep gap: DEGRADED with the reason, then recovery
    m, _l, seen, _c = run([SOUND] * 60, sleep_at=30)
    hist = [h["state"] for h in m.snapshot()["history"]]
    reasons = [h["reason"] for h in m.snapshot()["history"]]
    assert any("asleep" in r for r in reasons) and m.state == "READY", f"6: sleep shown, then recovery ({hist})"
    # 7. the device fails mid-stream: DEGRADED, reopened, READY
    m, lst, seen, _c = run([SOUND] * 20 + ["error"] + [SOUND] * 30)
    reasons = [h["reason"] for h in m.snapshot()["history"]]
    assert any("microphone" in r for r in reasons) and m.state == "READY", f"7: device failure and recovery ({reasons})"


def privacy():
    # 8. while listening: no file opened for writing, no socket created
    real_open, real_socket = builtins.open, socket.socket
    bad = []

    def guard_open(f, mode="r", *a, **k):
        if any(c in mode for c in "wax+"):
            bad.append(("open", str(f), mode))
        return real_open(f, mode, *a, **k)

    def guard_socket(*a, **k):
        bad.append(("socket",))
        return real_socket(*a, **k)
    builtins.open, socket.socket = guard_open, guard_socket
    try:
        run([SOUND] * 20 + [SPEECH] * 10 + [SOUND] * 30, detector=Detector(wake_at={20}))
    finally:
        builtins.open, socket.socket = real_open, real_socket
    assert not bad, f"8: HARD FAIL - listening wrote or connected: {bad}"


def link_and_indicator():
    # 9. the clinic link is shown apart from listening and never blocks READY
    m = states.Machine()

    class DownLink:
        def whoami(self):
            raise LinkDown("clinic app unreachable")
    runtime.check_link(m, DownLink())
    snap = m.snapshot()
    assert snap["clinic"] == {"ok": False, "detail": "clinic app unreachable"} and m.state == "STARTING", \
        "9: a down clinic link is reported, not turned into a listening state"
    runtime.check_link(m, None)
    assert m.snapshot()["clinic"]["detail"] == "no device credential on this machine"
    # 10. the menu-bar label is the true state, or says the service is not running
    for state in states.STATES:
        assert state in jarvis_indicator.label({"state": state}), f"10: {state} label"
    assert "not running" in jarvis_indicator.label(None), "10: an unreachable service is never shown as READY"


def login_items():
    # 11. install and remove the login items, user scope only, reversible
    with tempfile.TemporaryDirectory() as tmp:
        agents = Path(tmp)
        calls = []
        launchd.install("/x/.venv/bin/python", "/x/Demo", "/x/Logs", agents, run=lambda cmd: calls.append(cmd), uid=501)
        names = sorted(p.name for p in agents.iterdir())
        assert names == [f"{launchd.INDICATOR_LABEL}.plist", f"{launchd.LABEL}.plist"], names
        assert all(c[:2] == ["launchctl", "bootstrap"] and c[2] == "gui/501" for c in calls), calls
        launchd.uninstall(agents, run=lambda cmd: calls.append(cmd), uid=501)
        assert list(agents.iterdir()) == [], "11: uninstall removes both plists"
        assert [c[1] for c in calls[-2:]] == ["bootout", "bootout"], "11: and unloads both"


def quiet_gate():
    # 12. idle CPU: the costly embedding is skipped while the whole window is at the noise floor, and recomputed exactly
    #     from kept melspectrogram frames when sound returns - every score that is computed equals the always-on one
    from jarvis.features import Features
    from jarvis.wake import Detector as RealDetector, WINDOW

    class Mel:
        def run(self, _o, feed):
            x = feed["input"][0][-1280:].reshape(8, 160)
            return [np.repeat(np.abs(x).mean(1, keepdims=True), 32, 1)[None, None]]

    class Emb:
        def __init__(self):
            self.calls = 0

        def run(self, _o, feed):
            self.calls += 1
            w = feed["input_1"][0, :, :, 0]
            return [np.concatenate([w.mean(0), w.max(0), w[-8:].mean(0)])[None, None, None]]

    class Model:
        threshold = 0.99

        def score(self, window):
            return float(np.tanh(np.asarray(window, np.float32).mean() / 500))

    rng = np.random.default_rng(3)
    loud = lambda: (rng.standard_normal(1280) * 3000).astype(np.int16)  # noqa: E731
    quiet = lambda: (rng.standard_normal(1280) * 20).astype(np.int16)  # noqa: E731
    script = [quiet() for _ in range(130)] + [loud() for _ in range(30)] + [quiet() for _ in range(80)] + \
        [loud() for _ in range(5)] + [quiet() for _ in range(40)]
    ref_f, ref = Features(sessions=(Mel(), Emb())), []
    from collections import deque
    win = deque(maxlen=WINDOW)
    for c in script:
        win.append(ref_f.feed(c))
        ref.append(Model().score(win) if len(win) == WINDOW else None)
    emb = Emb()
    det = RealDetector(Model(), Features(sessions=(Mel(), emb)))
    calls, skipped = [], []
    for i, c in enumerate(script):
        det.feed(c)
        calls.append(emb.calls)
        skipped.append(det.skipped)
        if not det.skipped and ref[i] is not None:
            assert abs(det.last_score - ref[i]) < 1e-9, f"12: score {i} differs from the always-on computation"
    assert not any(skipped[130:160]) and not any(skipped[240:245]), "12: never skipped while there is sound"
    assert sum(skipped[160:240]) >= 80 - WINDOW - 1, f"12: the quiet stretch is skipped ({sum(skipped[160:240])})"
    assert calls[239] - calls[160 + WINDOW] <= 1, "12: no embedding work during a long quiet stretch"
    assert calls[-1] < len(script) * 0.6, f"12: much less embedding work overall ({calls[-1]} of {len(script)})"


def selftest():
    ready_only_on_real_audio()
    wake_and_return()
    degraded_and_recovery()
    privacy()
    link_and_indicator()
    login_items()
    quiet_gate()
    print("jarvis_listen_selftest: ok")


if __name__ == "__main__":
    selftest()
    sys.exit(0)
