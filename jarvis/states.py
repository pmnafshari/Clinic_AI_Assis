"""The six states and the only moves between them (JARVIS plan §2). One source for the service, the page and tests."""
import threading
from collections import deque

import clinic_time

STATES = ("STARTING", "READY", "ACTIVE", "AUTH_REQUIRED", "CONFIRMING", "DEGRADED")
TRANSITIONS = {
    "STARTING": {"READY", "DEGRADED"},
    "READY": {"ACTIVE", "DEGRADED"},
    "ACTIVE": {"READY", "AUTH_REQUIRED", "CONFIRMING", "DEGRADED"},
    "AUTH_REQUIRED": {"READY", "DEGRADED"},
    "CONFIRMING": {"READY", "DEGRADED"},
    # recovery always starts again: DEGRADED never jumps straight back to READY
    "DEGRADED": {"STARTING"},
}
MEANING = {
    "STARTING": "starting: opening the microphone and the wake engine",
    "READY": "ready: only the wake phrase is listened for; nothing is recorded or sent",
    "ACTIVE": "listening to one request",
    "AUTH_REQUIRED": "this needs a staff member's signed-in session on this device",
    "CONFIRMING": "waiting for a spoken confirmation of an already-authorised action",
    "DEGRADED": "not available",
}


class BadTransition(Exception):
    pass


def allowed(a, b):
    return b in TRANSITIONS.get(a, set())


class Machine:
    """The companion's state. Thread-safe; waiters are woken on every change (the page's live stream)."""

    def __init__(self):
        self._lock = threading.Condition()
        self.state, self.reason, self.since = "STARTING", "starting up", clinic_time.to_storage(clinic_time.now_utc())
        self.version = 0
        self.clinic = {"ok": False, "detail": "not checked yet"}
        self.history = deque([{"state": self.state, "reason": self.reason, "at": self.since}], maxlen=20)

    def go(self, state, reason):
        if not reason:
            raise ValueError("every move needs a reason")
        with self._lock:
            if state == self.state and reason == self.reason:
                return
            if state != self.state and not allowed(self.state, state):
                raise BadTransition(f"{self.state} -> {state} is not allowed")
            self.state, self.reason = state, reason
            self.since = clinic_time.to_storage(clinic_time.now_utc())
            self.version += 1
            self.history.append({"state": state, "reason": reason, "at": self.since})
            self._lock.notify_all()

    def set_clinic(self, ok, detail):
        """The clinic link is shown beside the state, never as a listening state (J01)."""
        with self._lock:
            new = {"ok": bool(ok), "detail": detail}
            if new != self.clinic:
                self.clinic = new
                self.version += 1
                self._lock.notify_all()

    def snapshot(self):
        with self._lock:
            return {"state": self.state, "reason": self.reason, "since": self.since, "meaning": MEANING[self.state],
                    "version": self.version, "history": list(self.history), "clinic": dict(self.clinic)}

    def wait_change(self, version, timeout):
        """Block until the state changes from `version` or the timeout passes. -> snapshot."""
        with self._lock:
            self._lock.wait_for(lambda: self.version != version, timeout=timeout)
        return self.snapshot()
