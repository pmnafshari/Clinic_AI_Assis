"""One exchange (J02): the heard request -> local speech to text -> the clinic guides over loopback -> the answer on the
Jarvis page, with its citation. Nothing is spoken back (J04 is BLOCKED by POL-13). The transcript and the answer live in
memory only, on the page for ANSWER_SECONDS; the state's reason, which the history keeps, never carries what was said.

Since J-D10 the speech to text is hinted with the library's own terms, and what was heard is shown first: nothing is
asked until someone presses "Yes, ask this" on the page. No, a timeout or anything ambiguous discards it unasked.

Since J02 follow-up 6 an exchange is one numbered interaction running beside the listener: every write is made only
while that interaction is current (a new wake, DEGRADED or a refused device ends it), the device and its delegation are
checked again before what was heard is shown, every few seconds while it waits, and before it is asked; each kind of
failure has its own card; and answers are given only when switched on (JARVIS_GUIDE_ANSWERS=demo, jarvis_run.py).
"""
from jarvis.clinic import LinkDown, LinkRefused
from jarvis.stt import Cancelled, Failed, Unclear

ANSWER_SECONDS = 120
CONFIRM_SECONDS = 30
RECHECK_SECONDS = 4          # + the whoami call itself: a revoked device or ended delegation is noticed within 5 s
AGAIN = "Say the wake phrase and ask again."
WORKING = "working out what was asked (on this computer) - not recording"
ASKING = "asking the approved clinic guides - not recording"
PROBLEMS = {               # kind -> (the card's title, what to do next)
    "unclear": ("I could not make out the question", "Nothing was asked. " + AGAIN),
    "speech_to_text": ("Speech to text is not working on this computer",
                       "Nothing was asked. Tell whoever looks after this computer."),
    "clinic_down": ("The clinic app could not be reached",
                    "Nothing was asked. Check that the clinic app is running, then ask again."),
    "not_set_up": ("Clinic-guide answers are not set up on this computer",
                   "Nothing was asked. An admin registers this computer under Admin > Jarvis devices."),
    "refused": ("The clinic app refused this computer",
                "Nothing was asked. An admin can register this computer again (Admin > Jarvis devices)."),
    "switched_off": ("Clinic-guide answers are switched off on this computer", "Nothing was transcribed or asked."),
}
DISCARDED = {
    "discard": "You pressed No. Nothing was asked. " + AGAIN,
    "timeout": "Not confirmed within {seconds} seconds. Nothing was asked. " + AGAIN,
    "ambiguous": "The confirmation was not clear (more than one choice reached Jarvis). Nothing was asked. " + AGAIN,
}


class Stop(Exception):
    """This interaction is over; `reason` says why (no speech in it)."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def identity(who):
    """What a confirmation was given under: the device and whoever's session is delegated to it (J00)."""
    d = who.get("delegation")
    return who.get("device_id"), (d or {}).get("username"), (d or {}).get("expires_at")


class Exchange:
    def __init__(self, machine, transcribe, link_factory, enabled=True, confirm_seconds=CONFIRM_SECONDS,
                 recheck_seconds=RECHECK_SECONDS):
        self.machine, self.transcribe, self.link_factory = machine, transcribe, link_factory
        self.enabled, self.confirm_seconds, self.recheck_seconds = enabled, confirm_seconds, recheck_seconds

    def __call__(self, pcm, turn):
        """-> the reason for going back to READY (the caller ends the interaction with it)."""
        try:
            return self.handle(pcm, turn)
        except Stop as e:
            return e.reason

    def handle(self, pcm, turn):
        m = self.machine
        if not self.enabled:
            return self.problem(turn, "switched_off", "J02 is not accepted for use; listening is not affected")
        if not m.go_for(turn, "ACTIVE", WORKING):
            return "cancelled"
        link = self.link_factory()
        if link is None:
            return self.problem(turn, "not_set_up", "no device credential (Admin > Jarvis devices)")
        given = identity(self.call(turn, link.whoami))
        terms = self.call(turn, link.vocabulary)
        try:
            heard, _language = self.transcribe(pcm, terms, cancelled=lambda: not m.current(turn))
        except Cancelled:
            return "cancelled"
        except Unclear as e:
            return self.problem(turn, "unclear", str(e))
        except Failed as e:
            return self.problem(turn, "speech_to_text", str(e))
        self.recheck(turn, link, given)
        pid = m.offer(turn, heard, self.confirm_seconds)
        if pid is None:
            return "cancelled"
        decision = self.wait(turn, pid, link, given)
        if decision == "cancelled":
            return "cancelled"
        if decision != "ask":
            m.show_for(turn, {"outcome": "discarded", "heard": None, "citations": [], "warnings": [],
                              "message": DISCARDED[decision].format(seconds=self.confirm_seconds)}, ANSWER_SECONDS)
            return {"discard": "what was heard was not confirmed - nothing was asked",
                    "timeout": "nobody confirmed what was heard in time - nothing was asked"}.get(
                decision, "the confirmation was not clear - nothing was asked")
        self.recheck(turn, link, given)
        if not m.go_for(turn, "ACTIVE", ASKING) or not m.sending(turn):
            return "cancelled"
        try:
            result = link.ask_guides(heard)
        except LinkRefused as e:
            return self.problem(turn, "refused", str(e), heard)
        except LinkDown as e:
            return self.problem(turn, "clinic_down", str(e), heard)
        if not m.show_for(turn, {**result, "heard": heard}, ANSWER_SECONDS):
            return "cancelled"
        if result["outcome"] == "answer":
            c = result["citations"][0]
            return f"answered on screen ({c['title']}, page {c['page']})"
        return f"a refusal is on screen ({result['reason']})"

    def call(self, turn, fn):
        """A clinic call before anything was heard; a refusal or an outage ends the interaction with its card."""
        try:
            return fn()
        except LinkRefused as e:
            raise Stop(self.problem(turn, "refused", str(e))) from None
        except LinkDown as e:
            raise Stop(self.problem(turn, "clinic_down", str(e))) from None

    def recheck(self, turn, link, given):
        """The device must still be accepted, under the same delegation, or the interaction is cancelled."""
        try:
            now = identity(link.whoami())
        except LinkRefused:
            self.machine.cancel(turn, "this device was refused by the clinic app (revoked or not registered)")
            raise Stop("cancelled - the device was refused") from None
        except LinkDown:
            return                             # the ask itself will say the clinic app is down
        if now != given:
            self.machine.cancel(turn, "the staff session delegated to this device ended or changed")
            raise Stop("cancelled - the delegation changed")

    def wait(self, turn, pid, link, given):
        """The decision on the card, rechecking the device every few seconds while nobody has chosen."""
        while True:
            decision = self.machine.await_decision(pid, within=self.recheck_seconds)
            if decision is not None:
                return decision
            self.recheck(turn, link, given)

    def problem(self, turn, kind, why, heard=None):
        """A card saying what went wrong and what became of the question. -> the reason (no speech in it)."""
        title, then = PROBLEMS[kind]
        if heard is not None:
            then = "The question was not answered."
        self.machine.show_for(turn, {"outcome": "unclear" if kind == "unclear" else "unavailable", "problem": kind,
                                     "title": title, "heard": heard, "citations": [], "warnings": [],
                                     "message": f"{title}: {why}. {then}"}, ANSWER_SECONDS)
        return {"unclear": "did not catch the question - nothing was asked",
                "switched_off": "request heard and discarded - clinic-guide answers are switched off"}.get(
            kind, f"could not ask the clinic guides ({why})")
