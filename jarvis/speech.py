"""Spoken replies for the synthetic demo (J04, started for this scope only). Owner decision 2026-10-11 on P14-D2 /
POL-13: the built-in macOS voice (/usr/bin/say - offline, nothing downloaded) reads the card Jarvis shows after an
exchange, only while clinic-guide answers are on for the synthetic demo (JARVIS_GUIDE_ANSWERS=demo). Outside the demo
nothing is spoken. A guide answer holds no patient data; patient-specific speech needs the room-privacy rule first.

A card is read once, after the exchange is over (READY); a new wake phrase, a pause or DEGRADED stops the voice at once.
"""
import subprocess
import threading

SAY = "/usr/bin/say"
MAX_CHARS = 600


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
            parts.append(f"From {c['title']}, page {c['page']}.")
        for w in card.get("warnings") or []:
            parts.append(f"Warning: {w['text']}")
        return " ".join(parts) or None
    title, message = card.get("title"), card.get("message")
    if title and message and message.startswith(title):
        return message
    return " ".join(x for x in (title, message) if x) or None


class Speaker:
    def __init__(self, popen=subprocess.Popen):
        self.popen, self.proc, self.lock = popen, None, threading.Lock()

    def say(self, card):
        text = words(card)
        self.stop()
        if not text:
            return False
        with self.lock:
            self.proc = self.popen([SAY, "--", text[:MAX_CHARS]], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        return True

    def stop(self):
        with self.lock:
            p, self.proc = self.proc, None
        if p is not None and p.poll() is None:
            p.terminate()


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
