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


# The embedding network's layer shapes (openWakeWord's speech_embedding): (kind, kernel time, kernel freq, freq pad) or
# (pool, time). Random small weights stand in for the real ones; no model file is read.
SHAPES = [("conv", 3, 3, 1), ("conv", 1, 3, 1), ("conv", 3, 1, 0), ("pool", 2),
          ("conv", 1, 3, 1), ("conv", 3, 1, 0), ("conv", 1, 3, 1), ("conv", 3, 1, 0), ("pool", 1),
          ("conv", 1, 3, 1), ("conv", 3, 1, 0), ("conv", 1, 3, 1), ("conv", 3, 1, 0), ("pool", 2),
          ("conv", 1, 3, 1), ("conv", 3, 1, 0), ("conv", 1, 3, 1), ("conv", 3, 1, 0), ("pool", 1),
          ("conv", 1, 3, 1), ("conv", 3, 1, 0), ("conv", 1, 3, 1), ("conv", 3, 1, 0), ("pool", 2),
          ("last", 3, 1, 0)]


def tiny_layers(seed=0, width=3, out=6):
    rng = np.random.default_rng(seed)
    layers, cin = [], 1
    for k in SHAPES:
        if k[0] == "pool":
            layers.append(("pool", k[1]))
            continue
        cout = out if k[0] == "last" else width
        w = (rng.standard_normal((cout, cin, k[1], k[2])) * 0.6).astype(np.float32)
        b = (rng.standard_normal(cout) * 0.1).astype(np.float32) if k[0] != "last" else np.zeros(cout, np.float32)
        layers.append(("conv", w, b, k[3], k[0] != "last"))
        cin = cout
    return layers


def window_embedding(layers, window):
    """The plain per-window computation, written independently of jarvis.embedding: [76, 32] -> [out]."""
    x = np.asarray(window, np.float64)[:, :, None]                     # time, freq, channels
    for layer in layers:
        if layer[0] == "pool":
            t, f, c = x.shape
            kt = layer[1]
            x = x[:t - t % kt].reshape(t // kt, kt, f // 2, 2, c).max(axis=(1, 3))
            continue
        _, w, b, pf, act = layer
        cout, cin, kt, kf = w.shape
        xp = np.pad(x, ((0, 0), (pf, pf), (0, 0)))
        t, f = xp.shape[0] - kt + 1, xp.shape[1] - kf + 1
        y = np.zeros((t, f, cout)) + b
        for a in range(kt):
            for c in range(kf):
                y += np.einsum("tfi,oi->tfo", xp[a:a + t, c:c + f], w[:, :, a, c])
        if act:
            y = np.maximum(np.where(y > 0, y, 0.2 * y), -0.4)
        x = y
    assert x.shape[:2] == (1, 1), x.shape
    return x.reshape(-1)


class FakeMel:
    """10 ms frames from the samples after the 480-sample context: 8 frames per 80 ms chunk, like the real one."""

    def run(self, _o, feed):
        x = feed["input"][0][480:].reshape(-1, 160)
        return [np.repeat(np.abs(x).mean(1, keepdims=True) / 500, 32, 1)[None, None]]


def quiet_gate():
    # 12. idle CPU: the quiet gate skips only while the whole window is at the room's noise floor, and every score that
    #     is computed equals the always-on computation (J01 follow-up: the embedding is now cheap and always computed,
    #     so the gate skips the classifier; its decisions are unchanged)
    from jarvis.embedding import Stream
    from jarvis.features import Features
    from jarvis.wake import Detector as RealDetector, WINDOW

    class Model:
        threshold = 0.99

        def score(self, window):
            return float(np.tanh(np.asarray(window, np.float32).mean() * 3))

    rng = np.random.default_rng(3)
    loud = lambda: (rng.standard_normal(1280) * 3000).astype(np.int16)  # noqa: E731
    quiet = lambda: (rng.standard_normal(1280) * 20).astype(np.int16)  # noqa: E731
    script = [quiet() for _ in range(130)] + [loud() for _ in range(30)] + [quiet() for _ in range(80)] + \
        [loud() for _ in range(5)] + [quiet() for _ in range(40)]
    layers = tiny_layers()
    ref_f = Features(mel=FakeMel(), stream=Stream(layers))
    from collections import deque
    win, ref = deque(maxlen=WINDOW), []
    for c in script:
        win.append(ref_f.add(c)[0])
        ref.append(Model().score(win) if len(win) == WINDOW else None)
    det = RealDetector(Model(), Features(mel=FakeMel(), stream=Stream(layers)))
    skipped, scored = [], 0
    for i, c in enumerate(script):
        det.feed(c)
        skipped.append(det.skipped)
        if i % 2 == 1 and not det.skipped and ref[i] is not None:
            assert abs(det.last_score - ref[i]) < 1e-6, f"12: score {i} differs from the always-on computation"
            scored += 1
    assert scored >= 40, f"12: scores were compared ({scored})"
    assert not any(skipped[131:160]) and not any(skipped[241:245]), "12: never skipped while there is sound"
    assert sum(skipped[160:240]) >= 80 - WINDOW - 2, f"12: the quiet stretch is skipped ({sum(skipped[160:240])})"


def streaming_embedding():
    # 13. the embedding is computed as a stream: each 80 ms window's result equals the plain per-window computation of
    #     the same network, and each block costs only its new frames - never the whole 76-frame window again
    from jarvis import embedding
    from jarvis.embedding import HOP, WINDOW_FRAMES, Stream

    layers = tiny_layers(seed=1)
    rng = np.random.default_rng(13)
    frames = (rng.standard_normal((WINDOW_FRAMES + HOP * 70, 32)) * 1.5 + 1).astype(np.float32)
    first_rows = []
    real_conv = embedding.conv

    def counting_conv(x, layer):
        if x.shape[2] == 1:                       # the first convolution: one input channel
            first_rows.append(x.shape[0])
        return real_conv(x, layer)
    embedding.conv = counting_conv
    try:
        st = Stream(layers)
        got = [st.reset(frames[:WINDOW_FRAMES])]
        assert got[0].shape == (6,), f"13: one embedding for the priming window ({got[0].shape})"
        for k in range(30):                       # blocks of two chunks (16 frames)
            a = WINDOW_FRAMES + 2 * HOP * k
            first_rows.clear()
            out = st.push(frames[a:a + 2 * HOP])
            assert out.shape == (2, 6), f"13: two embeddings per 16 frames ({out.shape})"
            assert sum(first_rows) <= 2 * HOP + 2, f"13: block {k} recomputed {sum(first_rows)} rows, not just its new ones"
            got += list(out)
        mid = len(got)
        st.reset(frames[HOP * mid: HOP * mid + WINDOW_FRAMES])   # a reset starts a new stream at any window
        got2 = []
        for k in range(5):
            a = HOP * mid + WINDOW_FRAMES + HOP * k
            got2 += list(st.push(frames[a:a + HOP]))
    finally:
        embedding.conv = real_conv
    for i, e in enumerate(got):
        ref = window_embedding(layers, frames[HOP * i: HOP * i + WINDOW_FRAMES])
        assert np.abs(e - ref).max() < 1e-4, f"13: window {i} differs from the per-window computation"
    for i, e in enumerate(got2):
        ref = window_embedding(layers, frames[HOP * (mid + 1 + i): HOP * (mid + 1 + i) + WINDOW_FRAMES])
        assert np.abs(e - ref).max() < 1e-4, f"13: window {i} after a reset differs"

    # Features carries the melspectrogram's context and the stream from one call to the next (added after mutation
    # run 1: M24, restarting both on every call, survived)
    from jarvis.features import Features

    class ContextMel:
        """640-sample frames every 160 samples, reading into the 480-sample context like the real one."""

        def run(self, _o, feed):
            x = np.abs(feed["input"][0])
            n = (len(x) - 640) // 160 + 1
            return [np.repeat(np.array([x[j * 160:j * 160 + 640].mean() for j in range(n)])[:, None] / 500, 32, 1)[None, None]]
    audio = (rng.standard_normal(1280 * 12) * 2000).astype(np.int16)
    f = Features(mel=ContextMel(), stream=Stream(layers))
    got3 = np.concatenate([f.add(audio[k * 2560:(k + 1) * 2560]) for k in range(6)])
    whole = ContextMel().run(None, {"input": np.concatenate([np.zeros(480), audio])[None]})[0].reshape(-1, 32) / 10 + 2
    whole = np.concatenate([np.ones((WINDOW_FRAMES, 32)), whole])
    for i, e in enumerate(got3):
        ref = window_embedding(layers, whole[HOP * (i + 1): HOP * (i + 1) + WINDOW_FRAMES])
        assert np.abs(e - ref).max() < 1e-4, f"13: chunk {i} lost the context or the stream between calls"


def batching():
    # 14. features are computed for two chunks at a time: the detector wakes on the same chunk or one later than when
    #     fed one chunk at a time, never misses or adds a wake, and a reset drops a half-filled batch
    from jarvis import wake
    from jarvis.embedding import Stream
    from jarvis.features import Features

    class Model:
        threshold = 0.6

        def score(self, window):
            return float(np.asarray(window, np.float32)[-3:].mean() > 0.0)

    rng = np.random.default_rng(14)
    script = []
    for gap in (40, 41, 37, 44, 39, 42):           # bursts start on odd and even chunks
        script += [(rng.standard_normal(1280) * 30).astype(np.int16) for _ in range(gap)]
        script += [(rng.standard_normal(1280) * 4000).astype(np.int16) for _ in range(4)]
    layers = tiny_layers(seed=2, out=1)
    layers[-1] = ("conv", np.abs(layers[-1][1]), layers[-1][2], 0, False)

    def fired(batch):
        old = wake.BATCH
        wake.BATCH = batch
        try:
            det = wake.Detector(Model(), Features(mel=FakeMel(), stream=Stream(layers)))
            return [i for i, c in enumerate(script) if det.feed(c)]
        finally:
            wake.BATCH = old
    one, two = fired(1), fired(2)
    assert len(one) >= 4, f"14: the script wakes the per-chunk detector ({one})"
    assert {i % 2 for i in one} == {0, 1}, f"14: wakes fall on both halves of a batch ({one})"
    assert len(two) == len(one), f"14: same number of wakes batched ({two} vs {one})"
    assert all(b - a in (0, 1) for a, b in zip(one, two)), f"14: a batched wake is at most one chunk late ({two} vs {one})"

    # the melspectrogram is still computed one chunk per call: the real model floors each call at its own loudest
    # frame - 80 dB, so one call over two chunks changes the quieter one (added after E1 found exactly this)
    class FloorMel:
        def run(self, _o, feed):
            x = np.abs(feed["input"][0])
            n = (len(x) - 640) // 160 + 1
            db = 10 * np.log10(np.array([np.mean(x[j * 160:j * 160 + 640] ** 2) for j in range(n)]) + 1e-10)
            return [np.repeat(np.maximum(db, db.max() - 80)[:, None], 32, 1)[None, None]]
    quiet_then_loud = np.concatenate([(rng.standard_normal(1280) * 2).astype(np.int16),
                                      (rng.standard_normal(1280) * 20000).astype(np.int16)])
    together = Features(mel=FloorMel(), stream=Stream(layers)).add(quiet_then_loud)
    apart = Features(mel=FloorMel(), stream=Stream(layers))
    apart = np.concatenate([apart.add(quiet_then_loud[:1280]), apart.add(quiet_then_loud[1280:])])
    assert np.abs(together - apart).max() < 1e-4, "14: a chunk's features depend on the chunk computed with it"

    class Recorder:
        def __init__(self):
            self.sizes = []

        def reset(self):
            pass

        def add(self, samples):
            self.sizes.append(len(samples))
            return np.zeros((len(samples) // 1280, 6), np.float32)
    rec = Recorder()
    det = wake.Detector(Model(), rec)
    det.feed(script[0])
    det.reset()
    det.feed(script[1])
    assert rec.sizes == [], "14: a half batch is not computed early"
    det.feed(script[2])
    assert rec.sizes == [2 * 1280], f"14: a reset drops the pending half batch ({rec.sizes})"


def damaged_model():
    # 15. the embedding weights are read from the model file at start: a damaged or different file makes Jarvis
    #     DEGRADED with the reason (engine missing), never a crash loop and never a wrong network run
    import jarvis.features
    import jarvis_run
    from jarvis import onnx_layers
    with tempfile.TemporaryDirectory() as tmp:
        bad = Path(tmp) / "embedding_model.onnx"
        for content in (b"", b"not a model at all", bytes(range(256)) * 40, b"\x3a\x05\x0a\x03abc",
                        b"\x08\x80"):
            bad.write_bytes(content)
            try:
                onnx_layers.layers(bad)
            except ValueError:
                pass
            except Exception as e:
                raise AssertionError(f"15: a damaged model file escaped as {type(e).__name__}") from None
            else:
                raise AssertionError(f"15: a damaged model file was accepted ({content[:12]!r})")
    real = jarvis.features.Features

    def wrong_network():
        raise ValueError("embedding model is not the expected network: test")
    jarvis.features.Features = wrong_network
    try:
        jarvis_run._detector()
    except listen.EngineMissing as e:
        assert "not the expected network" in str(e), f"15: the reason is shown ({e})"
    except Exception as e:
        raise AssertionError(f"15: a wrong network escaped as {type(e).__name__}, not engine missing") from None
    else:
        raise AssertionError("15: a wrong network did not make the engine missing")
    finally:
        jarvis.features.Features = real


def selftest():
    ready_only_on_real_audio()
    wake_and_return()
    degraded_and_recovery()
    privacy()
    link_and_indicator()
    login_items()
    quiet_gate()
    streaming_embedding()
    batching()
    damaged_model()
    print("jarvis_listen_selftest: ok")


if __name__ == "__main__":
    selftest()
    sys.exit(0)
