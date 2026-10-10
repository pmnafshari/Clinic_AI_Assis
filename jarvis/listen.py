"""Always-ready listening (J01): microphone -> wake phrase -> one exchange -> back to READY.

READY is reported only after real sound has been processed. Idle audio lives in a 2 s ring buffer and is discarded;
the request heard after the wake phrase is held only until it is handed to the exchange (since J02: local speech to
text, then the clinic guides), then dropped. Nothing is written to disk or sent anywhere. macOS delivers digital silence
when the microphone is denied or muted, so a run of exact zeros means DEGRADED, never READY.

Since J02 follow-up 6 the exchange runs in its own thread and the listener never waits for it: the wake phrase is still
listened for, and the microphone and sleep still watched, while what was said is worked out, confirmed and asked. A new
wake starts a new interaction (the machine cancels the old one); DEGRADED cancels it too.
"""
import logging
import queue
import threading
import time
from collections import deque

import numpy as np

CHUNK_SECONDS = 0.08
BUFFER_SECONDS = 2.0
READY_AFTER_SECONDS = 1.0       # real sound processed before READY is claimed
ZERO_SECONDS = 2.0              # exact silence this long = denied or muted
STARVED_SECONDS = 3.0           # no audio arriving at all
SLEEP_GAP_SECONDS = 5.0         # wall clock ran ahead of the monotonic clock: the Mac slept
SILENCE_SECONDS = 1.2           # end of the request
IDLE_SECONDS = 6.0              # nothing said after the wake phrase
MAX_ACTIVE_SECONDS = 15.0
RETRY_SECONDS = 5.0


class MicError(Exception):
    pass


class EngineMissing(Exception):
    pass


def rms(chunk):
    return float(np.sqrt(np.mean(np.asarray(chunk, np.float32) ** 2)))


class Listener:
    def __init__(self, machine, make_detector, make_source, cue=None, mono=time.monotonic, wall=time.time, on_request=None):
        self.machine, self.make_detector, self.make_source = machine, make_detector, make_source
        self.on_request = on_request       # the exchange (J02); without one the request is discarded, as in J01
        self.cue, self.mono, self.wall = cue or (lambda: None), mono, wall
        self.buffer = deque(maxlen=int(round(BUFFER_SECONDS / CHUNK_SECONDS)))
        self.request = []
        self.recording = False             # the request after a wake, its own limits; not the machine's state
        self.turn = 0
        self.detector = self.source = None
        self.good = self.zeros = self.starved = 0
        self.retry_at = 0.0
        self.last = None

    @property
    def heard_bytes(self):
        return sum(c.nbytes for c in self.request)

    # --- state helpers -------------------------------------------------------------------------------------
    def degrade(self, reason):
        self.request.clear()
        self.buffer.clear()
        self.recording = False
        self.good = 0
        if self.machine.state != "DEGRADED" or self.machine.reason != reason:
            self.machine.go("DEGRADED", reason)     # cancels the interaction in progress, if any

    def become_ready(self, reason):
        if self.machine.state == "DEGRADED":
            self.machine.go("STARTING", "recovering")
        if self.machine.state == "STARTING":
            self.machine.go("READY", reason)

    def end_recording(self, reason=None):
        """The request is over: hand it to the exchange, or (J01, no exchange, too long, nothing said) end the
        interaction with `reason`. Either way its audio leaves the listener here."""
        pcm = np.concatenate(self.request) if self.request and reason is None else None
        self.request.clear()
        self.buffer.clear()
        self.recording = False
        if self.detector:
            self.detector.reset()
        if pcm is None:
            return self.machine.finish(self.turn, reason)
        turn, exchange = self.turn, self.on_request

        def work():
            try:
                reason = exchange(pcm, turn)
            except Exception as e:                 # a fault ends this interaction, never the listening
                logging.exception("exchange fault")
                reason = f"the exchange failed ({type(e).__name__}) - nothing more is shown for it"
            self.machine.finish(turn, reason)
        threading.Thread(target=work, name=f"jarvis-exchange-{turn}", daemon=True).start()

    # --- lifecycle -----------------------------------------------------------------------------------------
    def start(self):
        self._load_engine()

    def _load_engine(self):
        try:
            self.detector = self.make_detector()
        except EngineMissing as e:
            self.detector = None
            self.degrade(f"wake engine missing: {e}")
        self.retry_at = self.mono() + RETRY_SECONDS

    def _open(self):
        try:
            self.source = self.make_source()
            self.source.open()
        except MicError as e:
            self.source = None
            self.degrade(str(e))

    def step(self):
        """One chunk. Raises StopIteration only when a test source runs out."""
        if self.source is None:
            if self.mono() >= self.retry_at or self.machine.state != "DEGRADED":
                self._open()
                self.retry_at = self.mono() + RETRY_SECONDS
            if self.source is None:
                return
        try:
            chunk = self.source.read(timeout=0.5)
        except MicError as e:
            self.source.close()
            self.source = None
            return self.degrade(f"the microphone stopped ({e})")
        if self._slept():
            return
        if chunk is None:
            self.starved += 1
            if self.starved * 0.5 >= STARVED_SECONDS:
                self.degrade("no audio is arriving from the microphone")
            return
        self.starved = 0
        if self.detector is None:
            if self.mono() >= self.retry_at:
                self._load_engine()
            return
        self._sound(chunk)

    def _slept(self):
        now = (self.mono(), self.wall())
        last, self.last = self.last, now
        if last and (now[1] - last[1]) - (now[0] - last[0]) > SLEEP_GAP_SECONDS:
            asleep = time.strftime("%H:%M", time.localtime(last[1]))
            woke = time.strftime("%H:%M", time.localtime(now[1]))
            self.detector and self.detector.reset()
            self.degrade(f"the computer was asleep ({asleep}-{woke}); nothing was heard meanwhile")
            return True
        return False

    def _sound(self, chunk):
        if not np.any(chunk):
            self.zeros += 1
            if self.zeros * CHUNK_SECONDS >= ZERO_SECONDS:
                self.degrade("the microphone is muted or access denied (macOS delivers silence)")
            return
        self.zeros = 0
        if self.recording:
            return self._record(chunk)
        state = self.machine.state
        self.buffer.append(chunk)
        woke = self.detector.feed(chunk)
        if state in ("STARTING", "DEGRADED"):
            self.good += 1
            if self.good * CHUNK_SECONDS >= READY_AFTER_SECONDS:
                self.become_ready("listening for the wake phrase")
            return
        # READY, or an exchange working or waiting for its confirmation: a wake starts a new interaction
        if woke and state in ("READY", "ACTIVE", "WAITING_FOR_CONFIRMATION"):
            self.floor = float(np.median([rms(c) for c in self.buffer])) if self.buffer else 0.0
            self.buffer.clear()
            self.turn = self.machine.begin("heard the wake phrase - listening to the request")
            self.recording = True
            self.cue()
            self.active_since, self.last_speech = self.mono(), None

    def _record(self, chunk):
        now = self.mono()
        self.request.append(chunk)
        if rms(chunk) > max(2.5 * self.floor, 300.0):
            self.last_speech = now
        if self.last_speech is not None and now - self.last_speech >= SILENCE_SECONDS:
            if self.on_request is None:
                spoken = len(self.request) * CHUNK_SECONDS
                return self.end_recording(f"request heard ({spoken:.1f} s) and discarded - answers arrive in J02")
            return self.end_recording()
        if self.last_speech is None and now - self.active_since >= IDLE_SECONDS:
            return self.end_recording("nothing was said after the wake phrase")
        if now - self.active_since >= MAX_ACTIVE_SECONDS:
            return self.end_recording("request too long - stopped listening")

    def run(self, stop):
        self.start()
        while not stop.is_set():
            try:
                self.step()
            except StopIteration:
                return


class SoundDeviceSource:
    """The default input device at 16 kHz mono, through PortAudio. Nothing is kept beyond the queue."""

    def __init__(self):
        self.stream = None
        self.q = queue.Queue(maxsize=64)

    def open(self):
        import sounddevice as sd
        try:
            sd.query_devices(kind="input")
            self.stream = sd.InputStream(samplerate=16000, channels=1, dtype="int16", blocksize=1280, callback=self._cb)
            self.stream.start()
        except Exception as e:
            raise MicError(f"no microphone available ({type(e).__name__})") from None

    def _cb(self, indata, frames, when, status):
        try:
            self.q.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass

    def read(self, timeout):
        if self.stream is not None and not self.stream.active:
            raise MicError("input stream ended")
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self):
        try:
            if self.stream is not None:
                self.stream.close()
        finally:
            self.stream = None


def chime():
    """The listening cue: two short tones, generated (not speech, so not TTS)."""
    import sounddevice as sd
    t = np.arange(int(0.12 * 16000)) / 16000
    tone = np.concatenate([np.sin(2 * np.pi * 880 * t), np.sin(2 * np.pi * 1320 * t)]) * 0.2
    try:
        sd.play(tone.astype(np.float32), 16000, blocking=False)
    except Exception:
        pass  # no output device: the state change on the page and the indicator still show it
