"""The states and the only moves between them (JARVIS plan §2, §18). One source for the service, the page and tests.

Since J02 follow-up 6 every wake starts a numbered interaction. The exchange runs beside the listener, so everything it
writes (its state, its card, its answer, its end) carries that number and is dropped once the interaction is no longer
the current one: a new wake, DEGRADED or a refused device cancels it, and nothing it finishes later is shown or asked.
"""
import secrets
import threading
import time
from collections import deque

import clinic_time

STATES = ("STARTING", "READY", "ACTIVE", "WAITING_FOR_CONFIRMATION", "AUTH_REQUIRED", "CONFIRMING", "DEGRADED")
TRANSITIONS = {
    "STARTING": {"READY", "DEGRADED"},
    "READY": {"ACTIVE", "DEGRADED"},
    "ACTIVE": {"READY", "WAITING_FOR_CONFIRMATION", "AUTH_REQUIRED", "CONFIRMING", "DEGRADED"},
    "WAITING_FOR_CONFIRMATION": {"ACTIVE", "READY", "DEGRADED"},
    "AUTH_REQUIRED": {"READY", "DEGRADED"},
    "CONFIRMING": {"ACTIVE", "READY", "DEGRADED"},   # J03: a new wake phrase cancels a note on screen
    # recovery always starts again: DEGRADED never jumps straight back to READY
    "DEGRADED": {"STARTING"},
}
MEANING = {
    "STARTING": "starting: opening the microphone and the wake engine",
    "READY": "ready: only the wake phrase is listened for; nothing is recorded or sent",
    "ACTIVE": "one request: recording it after the wake phrase (at most 15 s), then working it out on this computer",
    "WAITING_FOR_CONFIRMATION": "what was heard is on screen; nothing is asked until someone confirms it there - "
                                "not recording, and the wake phrase is still heard",
    "AUTH_REQUIRED": "this needs a staff member's signed-in session on this device",
    "CONFIRMING": "a note, a change or a choice of patient is on screen; nothing is saved or opened until someone "
                  "picks the patient there",
    "DEGRADED": "not available",
}
WAIT_SLICE = 1.0           # a wait wakes at least this often, so a deadline passed during sleep is seen at once


def awake_clock():
    """Seconds on a clock that keeps counting while the Mac sleeps (Apple's clock_gettime(3): CLOCK_MONOTONIC_RAW
    continues to increment while the system is asleep; time.monotonic, mach_absolute_time, does not)."""
    return time.clock_gettime(time.CLOCK_MONOTONIC_RAW)


class BadTransition(Exception):
    pass


def allowed(a, b):
    return b in TRANSITIONS.get(a, set())


def cancelled_card(why, sent):
    if sent:
        message = f"Cancelled: {why}. The question had already gone to the clinic guides; its answer is not shown."
    else:
        message = f"Cancelled: {why}. Nothing was asked. Say the wake phrase and ask again."
    return {"outcome": "cancelled", "heard": None, "message": message, "citations": [], "warnings": []}


BUSY = {"ACTIVE", "WAITING_FOR_CONFIRMATION", "CONFIRMING", "AUTH_REQUIRED"}
WAITING = ("confirm", "note_confirm", "edit_confirm", "patient_pick")     # cards that wait for a click


def status_words(state, reason, paused):
    """What the page shows in large type - Ready, Active or Sleep - and a few words under it."""
    if paused:
        return {"word": "Sleep", "why": "Paused"}
    if state == "READY":
        return {"word": "Ready", "why": 'Say "Hey Jarvis"'}
    if state in BUSY:
        if state == "WAITING_FOR_CONFIRMATION":
            return {"word": "Active", "why": "Check what I heard"}
        if state == "CONFIRMING":
            return {"word": "Active", "why": "Pick the patient on screen"}
        return {"word": "Active", "why": "Listening" if reason.startswith("heard the wake phrase") else "Working on it"}
    if state == "STARTING":
        return {"word": "Sleep", "why": "Starting up"}
    return {"word": "Sleep", "why": reason}


def readiness(state, reason, answers):
    """The page's one overall line (J02 UI review): ready for the demo only while listening is READY and every
    clinic-guide prerequisite holds; otherwise what is missing, from the answers line, which says who can fix it."""
    if not answers.get("demo"):
        return {"ok": False, "text": "Clinic-guide answers are switched off on this computer - Jarvis only listens "
                                     "for its wake phrase (J02 is not accepted for use)."}
    if not answers["on"]:
        return {"ok": False, "text": "Not ready for the demo: " + answers.get("missing", answers["detail"]) + "."}
    if state in ("ACTIVE", "WAITING_FOR_CONFIRMATION"):
        return {"ok": True, "text": "A question is in progress."}
    if state != "READY":
        return {"ok": False, "text": f"Not ready for the demo: listening is {state} ({reason})."}
    return {"ok": True, "text": 'Ready for the controlled demo - say "Hey Jarvis", then a question about the synthetic '
                                "clinic guides."}


class Machine:
    """The companion's state. Thread-safe; waiters are woken on every change (the page's live stream)."""

    def __init__(self, clock=awake_clock):
        self._lock = threading.Condition()
        self.clock = clock         # deadlines (the confirmation, the answer on screen) - counts sleep
        self.slice = WAIT_SLICE
        self.state, self.reason, self.since = "STARTING", "starting up", clinic_time.to_storage(clinic_time.now_utc())
        self.version = 0
        self.clinic = {"ok": False, "detail": "not checked yet"}
        self.answers = {"on": False, "detail": "not checked yet", "demo": False}
        self.history = deque([{"state": self.state, "reason": self.reason, "at": self.since}], maxlen=20)
        self.turn = 0              # the interaction in progress; 0 = none
        self._turns = 0
        self._sent = False         # the current interaction's question has gone to the clinic guides
        self._answer = None        # (what to show, expiry on self.clock) - memory only (J02)
        self._pending = None       # what was heard, waiting for its on-screen confirmation: {id, decision, deadline}
        self.paused = False
        self.access = None         # the doctor this device acts for, as the clinic says (2026-10-11), or None
        self.access_epoch = 0      # changes with every sign-in and sign-out
        self.mic_level = 0.0       # the microphone's loudness now (0-1), for the page's animation only - not a state        # listening switched off by the owner (Clinic Workspace, P28) until resumed

    # --- moves ------------------------------------------------------------------------------------------------
    def _move(self, state, reason, again=False):
        if not reason:
            raise ValueError("every move needs a reason")
        if state == self.state and reason == self.reason and not again:
            return
        if state != self.state and not allowed(self.state, state):
            raise BadTransition(f"{self.state} -> {state} is not allowed")
        if state == "DEGRADED":
            self._cancel(reason)
        self.state, self.reason = state, reason
        self.since = clinic_time.to_storage(clinic_time.now_utc())
        self.history.append({"state": state, "reason": reason, "at": self.since})
        self._changed()

    def _changed(self):
        self.version += 1
        self._lock.notify_all()

    def _cancel(self, why):
        if not self.turn:
            return False
        self._answer = (cancelled_card(why, self._sent), self.clock() + 120)
        self.turn, self._pending, self._sent = 0, None, False
        self._changed()
        return True

    def go(self, state, reason):
        with self._lock:
            self._move(state, reason)

    def begin(self, reason):
        """A wake phrase: a new interaction, in ACTIVE. Whatever was still in flight is cancelled. -> its number."""
        with self._lock:
            if not self._cancel("a new wake phrase was heard"):
                self._answer = None            # a new request: the last answer goes
            self._turns += 1
            self.turn = self._turns
            self._move("ACTIVE", reason, again=True)       # a second wake is a new interaction, in the history too
            return self.turn

    def current(self, turn):
        with self._lock:
            return bool(turn) and turn == self.turn

    def go_for(self, turn, state, reason):
        """A move made by interaction `turn`, only while it is the current one. -> whether it was made."""
        with self._lock:
            if not turn or turn != self.turn:
                return False
            self._move(state, reason)
            return True

    def finish(self, turn, reason):
        """Interaction `turn` is over: back to READY, unless something else has taken over since."""
        with self._lock:
            if not turn or turn != self.turn:
                return False
            self.turn, self._pending, self._sent = 0, None, False
            self._move("READY", reason)
            return True

    def cancel(self, turn, why):
        """End interaction `turn` if it is still the current one (a refused device, a changed delegation), and go back
        to READY: nothing waits any more. (Without the move the state stayed WAITING_FOR_CONFIRMATION until the next
        wake - the exchange's own finish() is ignored once its turn is no longer current; found by the J02 UI review.)"""
        with self._lock:
            if not (bool(turn) and turn == self.turn and self._cancel(why)):
                return False
            self._move("READY", f"cancelled - {why}")
            return True

    def sending(self, turn):
        """The one moment the current interaction's question may go to the clinic guides - once. -> may it go."""
        with self._lock:
            if not turn or turn != self.turn or self._sent:
                return False
            self._sent = True
            return True

    # --- what the page shows -----------------------------------------------------------------------------------
    def set_clinic(self, ok, detail):
        """The clinic link is shown beside the state, never as a listening state (J01)."""
        with self._lock:
            new = {"ok": bool(ok), "detail": detail}
            if new != self.clinic:
                self.clinic = new
                self._changed()

    def set_answers(self, on, detail, demo=False, missing=None):
        """Whether clinic-guide answers are switched on (`demo`) and can be given here (`on`) - apart from listening
        (R2); `missing` says what stops them and who can fix it."""
        with self._lock:
            new = {"on": bool(on), "detail": detail, "demo": bool(demo)}
            if missing:
                new["missing"] = missing
            if new != self.answers:
                self.answers = new
                self._changed()

    def set_access(self, who):
        """Who the clinic says this device acts for - {username, role, expires_at} - or None (signed out, 00:00)."""
        with self._lock:
            new = {k: who.get(k) for k in ("username", "role", "expires_at", "kind")} if who else None
            if new != self.access:
                self.access = new
                self.access_epoch += 1          # whatever Jarvis remembered for the previous sign-in is gone
                self._changed()

    def set_paused(self, on):
        """The owner pauses or resumes listening; the listener closes or reopens the microphone (P28)."""
        with self._lock:
            if bool(on) != self.paused:
                self.paused = bool(on)
                self._changed()

    def show(self, answer, seconds):
        """An answer (or refusal) for the page, for `seconds`. Never in the history."""
        with self._lock:
            self._answer = (answer, self.clock() + seconds)
            self._changed()

    def show_for(self, turn, answer, seconds):
        with self._lock:
            if not turn or turn != self.turn:
                return False
            self._answer = (answer, self.clock() + seconds)
            self._changed()
            return True

    # --- the on-screen confirmation (J-D10) --------------------------------------------------------------------
    def offer(self, turn, heard, seconds):
        """Put what was heard on screen for confirmation. -> its id, which only the page can read; None if `turn`
        is no longer current."""
        with self._lock:
            if not turn or turn != self.turn:
                return None
            pid = secrets.token_urlsafe(16)
            deadline = self.clock() + seconds
            self._pending = {"id": pid, "decision": None, "deadline": deadline}
            self._answer = ({"outcome": "confirm", "heard": heard, "id": pid, "seconds": seconds}, deadline)
            self._move("WAITING_FOR_CONFIRMATION", "waiting for what was heard to be confirmed on screen")
            return pid

    def offer_note(self, turn, card, seconds, outcome="note_confirm", verb="save",
                   reason="a note is on screen, waiting to be saved there"):
        """A drafted note on screen (J03) - or a phone change ("edit_confirm"), or patients to open ("patient_pick",
        verb "open"; J03 slice 2): only a click on one of its patients ("<verb>:<cf>") or "discard" decides.
        -> its id; None if `turn` is no longer current."""
        with self._lock:
            if not turn or turn != self.turn:
                return None
            pid = secrets.token_urlsafe(16)
            deadline = self.clock() + seconds
            allowed = {"discard"} | {f"{verb}:" + c["cf"] for c in card["choices"]}
            self._pending = {"id": pid, "decision": None, "deadline": deadline, "allowed": allowed}
            self._answer = ({**card, "outcome": outcome, "id": pid, "seconds": seconds}, deadline)
            self._move("CONFIRMING", reason)
            return pid

    def decide(self, pid, decision):
        """The person's choice on the page. -> "taken" only for the first "ask" or "discard" on what is waiting now,
        before its deadline; "expired" after it; "gone" when nothing waits under that id; "ambiguous" for a second
        choice or an unknown word. A choice for another id makes what is waiting ambiguous."""
        with self._lock:
            p = self._pending
            if p is None or p["id"] != pid:
                if p is not None:
                    p["decision"] = "ambiguous"
                    self._lock.notify_all()
                return "gone"
            self._lock.notify_all()
            if p["decision"] is None and self.clock() >= p["deadline"]:
                p["decision"] = "timeout"
            if p["decision"] == "timeout":
                return "expired"
            if p["decision"] is not None or decision not in p.get("allowed", ("ask", "discard")):
                p["decision"] = "ambiguous"
                return "ambiguous"
            p["decision"] = decision
            return "taken"

    def await_decision(self, pid, within=None):
        """Wait for the choice on `pid` until its deadline. -> "ask", "discard", "ambiguous", "timeout" or
        "cancelled" (a new wake, DEGRADED, a refused device), and what was waiting is then gone; or None when
        `within` seconds pass first and it still waits."""
        stop = None if within is None else time.monotonic() + within
        with self._lock:
            while True:
                p = self._pending
                if p is None or p["id"] != pid:
                    return "cancelled"
                if p["decision"] is None and self.clock() >= p["deadline"]:
                    p["decision"] = "timeout"
                if p["decision"] is not None:
                    self._pending = None
                    return p["decision"]
                wait = min(self.slice, max(p["deadline"] - self.clock(), 0.01))
                if stop is not None:
                    if time.monotonic() >= stop:
                        return None
                    wait = min(wait, max(stop - time.monotonic(), 0.01))
                self._lock.wait(wait)

    # --- reading --------------------------------------------------------------------------------------------------
    def _shown(self):
        if self._answer is None:
            return None
        answer, until = self._answer
        left = until - self.clock()
        if left <= 0:
            return None
        if answer["outcome"] in WAITING:                     # a page opened late says the real time left
            return {**answer, "left": max(1, round(left))}
        return answer

    def snapshot(self):
        with self._lock:
            return {"state": self.state, "reason": self.reason, "since": self.since, "meaning": MEANING[self.state],
                    "version": self.version, "history": list(self.history), "clinic": dict(self.clinic),
                    "answers": dict(self.answers), "answer": self._shown(), "paused": self.paused,
                    "status": status_words(self.state, self.reason, self.paused), "access": self.access,
                    "ready": readiness(self.state, self.reason, self.answers)}

    def wait_change(self, version, timeout):
        """Block until the state changes from `version` or the timeout passes. -> snapshot."""
        with self._lock:
            self._lock.wait_for(lambda: self.version != version, timeout=timeout)
        return self.snapshot()
