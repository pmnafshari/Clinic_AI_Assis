"""Spoken replies for the synthetic demo (J04, started for this scope only). Owner decision 2026-10-11 on P14-D2 /
POL-13: the built-in macOS voice (/usr/bin/say - offline, nothing downloaded) reads the card Jarvis shows after an
exchange, only while clinic-guide answers are on for the synthetic demo (JARVIS_GUIDE_ANSWERS=demo). Outside the demo
nothing is spoken. A guide answer holds no patient data; patient-specific speech needs the room-privacy rule first.

A card is read once, after the exchange is over (READY); a new wake phrase, a pause or DEGRADED stops the voice at once.
The voice is written to a temporary file first, so its loudness every 40 ms is known: the Jarvis page animates from it
and shows the words as captions while they are said. The file is deleted when it has been played.
"""
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import wave

import numpy as np

SAY = "/usr/bin/say"
AFPLAY = "/usr/bin/afplay"
MAX_CHARS = 600
STEP = 0.04
RATE = 180                   # words a minute - a little calmer than the default
LOCALES = ("en_GB", "en_US", "en_AU", "en_IE", "en_ZA", "en_IN")
VOICE_LINE = re.compile(r"^(.+?)\s+([a-z]{2}_[A-Z]{2})\s+#")


def pick_voice(listing, env):
    """The most natural English voice installed (`say -v '?'` lines): Premium, then Enhanced, British first. None:
    only compact voices - use the system voice (the one chosen in System Settings > Accessibility > Spoken Content).
    JARVIS_VOICE pins one."""
    if env.get("JARVIS_VOICE"):
        return env["JARVIS_VOICE"]
    best = None
    for line in listing:
        m = VOICE_LINE.match(line)
        if not m or m.group(2) not in LOCALES:
            continue
        name = m.group(1).strip()
        quality = 0 if "(Premium)" in name else 1 if "(Enhanced)" in name else None
        if quality is None:
            continue
        rank = (quality, LOCALES.index(m.group(2)))
        if best is None or rank < best[0]:
            best = (rank, name)
    return best[1] if best else None


def installed_voice():
    try:
        out = subprocess.run([SAY, "-v", "?"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return pick_voice(out.splitlines(), os.environ)


def words(card):
    """What to say for a card on the page, or None. (The confirmation card has no title or message - it is read on
    screen; Jarvis is never READY while it waits.)"""
    if not card:
        return None
    if card.get("outcome") == "answer":
        parts = []
        for c in card.get("citations") or []:
            if c.get("language") not in (None, "en"):
                parts.append("The answer is on screen, in Italian." if c.get("language") == "it"
                             else "The answer is on screen, in another language.")
            else:
                parts.append(c["passage"])
            parts.append(f"That is from the {c['title']}, page {c['page']}.")
        for w in card.get("warnings") or []:
            parts.append(f"Warning: {w['text']}")
        return " ".join(parts) or None
    title, message = card.get("title"), card.get("message")
    if title and message and message.startswith(title):
        return message
    return " ".join(x for x in (title, message) if x) or None


def synthesize(text, path, voice=None):
    """The voice into a WAV file. -> (loudness 0-1 every STEP seconds, STEP)."""
    cmd = [SAY, "-r", str(RATE), "-o", path, "--file-format=WAVE", "--data-format=LEI16@44100"]
    if voice:
        cmd += ["-v", voice]
    subprocess.run(cmd + ["--", text], check=True, timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with wave.open(path) as w:
        rate = w.getframerate()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
    n = max(1, int(rate * STEP))
    rms = [float(np.sqrt(np.mean(pcm[i:i + n] ** 2))) for i in range(0, len(pcm), n)]
    top = max(rms) if rms and max(rms) > 0 else 1.0
    return [round(min(1.0, x / top), 3) for x in rms], STEP


def playback(path):
    return subprocess.Popen([AFPLAY, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Speaker:
    def __init__(self, synth=None, play=playback, clock=time.monotonic):
        if synth is None:
            voice = installed_voice()                  # chosen once, when Jarvis starts
            synth = lambda text, path: synthesize(text, path, voice)  # noqa: E731
        self.synth, self.play, self.clock = synth, play, clock
        self.lock = threading.Lock()
        self.proc, self.now, self.gen = None, None, 0

    def say(self, card):
        text = words(card)
        self.stop()
        if not text:
            return False
        with self.lock:
            gen = self.gen
        threading.Thread(target=self._run, args=(gen, text[:MAX_CHARS]), name="jarvis-voice", daemon=True).start()
        return True

    def _run(self, gen, text):
        tmp = tempfile.mkdtemp(prefix="jarvis-voice-")
        try:
            path = os.path.join(tmp, "reply.wav")
            levels, step = self.synth(text, path)
            with self.lock:
                if gen != self.gen:                    # stopped while the voice was being made
                    return
                self.proc = proc = self.play(path)
                self.now = {"id": gen, "text": text, "started": self.clock(), "step": step, "levels": levels}
            proc.wait()
        except (OSError, subprocess.SubprocessError, wave.Error):
            pass                                       # no voice this time; the card is on screen regardless
        finally:
            with self.lock:
                if gen == self.gen:
                    self.proc, self.now = None, None
            shutil.rmtree(tmp, ignore_errors=True)

    def stop(self):
        with self.lock:
            self.gen += 1
            p, self.proc, self.now = self.proc, None, None
        if p is not None and p.poll() is None:
            p.terminate()

    def live(self):
        """What is being said now: its text, the loudness at this moment and how far along it is - or None."""
        with self.lock:
            n = self.now
        if not n:
            return None
        t = self.clock() - n["started"]
        lv = n["levels"]
        i = int(t / n["step"])
        total = len(lv) * n["step"]
        return {"id": n["id"], "text": n["text"], "level": lv[i] if 0 <= i < len(lv) else 0.0,
                "progress": round(min(1.0, t / total), 3) if total else 1.0}


def watch(machine, speaker, halt):
    """Reads each new card once Jarvis is back in READY; stops the voice the moment anything else happens."""
    last, version = None, None
    while not halt.is_set():
        snap = machine.wait_change(version, 1.0)
        version = snap["version"]
        if snap["state"] != "READY" or snap["paused"]:
            speaker.stop()
            continue
        card = snap["answer"]
        key = repr(card)
        if card and key != last:
            last = key
            speaker.say(card)
    speaker.stop()
