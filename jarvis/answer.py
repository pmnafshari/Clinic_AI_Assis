"""One exchange (J02): the heard request -> local speech to text -> the clinic guides over loopback -> the answer on the
Jarvis page, with its citation. Nothing is spoken back (J04 is BLOCKED by POL-13). The transcript and the answer live in
memory only, on the page for ANSWER_SECONDS; the state's reason, which the history keeps, never carries what was said.

Since J-D10 the speech to text is hinted with the library's own terms, and what was heard is shown first: nothing is
asked until someone presses "Yes, ask this" on the page. No, a timeout or anything ambiguous discards it unasked.
"""
import time

from jarvis.clinic import LinkDown, LinkRefused
from jarvis.stt import Unclear

ANSWER_SECONDS = 120
CONFIRM_SECONDS = 30
DISCARDED = "Nothing was asked. Say the wake phrase and ask again."


class Exchange:
    def __init__(self, machine, transcribe, link_factory, clock=time.monotonic, confirm_seconds=CONFIRM_SECONDS):
        self.machine, self.transcribe, self.link_factory, self.clock = machine, transcribe, link_factory, clock
        self.confirm_seconds = confirm_seconds

    def show(self, answer):
        self.machine.show(answer, ANSWER_SECONDS, clock=self.clock)

    def __call__(self, pcm):
        """-> the reason for going back to READY."""
        self.machine.go("ACTIVE", "working out what was asked (on this computer)")
        link = self.link_factory()
        if link is None:
            return self.unavailable("this computer has no Jarvis device credential (Admin > Jarvis devices)")
        try:
            terms = link.vocabulary()
        except (LinkDown, LinkRefused) as e:
            return self.unavailable(str(e))
        try:
            heard, _language = self.transcribe(pcm, terms)
        except Unclear as e:
            self.show({"outcome": "unclear", "heard": None, "message": f"I did not catch that: {e}. Nothing was asked.",
                       "citations": [], "warnings": []})
            return "did not catch the question - nothing was asked"
        pid = self.machine.offer(heard, self.confirm_seconds, clock=self.clock)
        self.machine.go("ACTIVE", "waiting for what was heard to be confirmed on screen")
        decision = self.machine.await_decision(pid, self.confirm_seconds)
        if decision != "ask":
            self.show({"outcome": "discarded", "heard": None, "message": DISCARDED, "citations": [], "warnings": []})
            return {"discard": "what was heard was not confirmed - nothing was asked",
                    "timeout": "nobody confirmed what was heard in time - nothing was asked"}.get(
                decision, "the confirmation was not clear - nothing was asked")
        self.machine.go("ACTIVE", "asking the approved clinic guides")
        try:
            result = link.ask_guides(heard)
        except (LinkDown, LinkRefused) as e:
            return self.unavailable(str(e), heard)
        self.show({**result, "heard": heard})
        if result["outcome"] == "answer":
            c = result["citations"][0]
            return f"answered on screen ({c['title']}, page {c['page']})"
        return f"a refusal is on screen ({result['reason']})"

    def unavailable(self, why, heard=None):
        self.show({"outcome": "unavailable", "heard": heard, "message": f"The clinic guides could not be asked: {why}.",
                   "citations": [], "warnings": []})
        return f"could not ask the clinic guides ({why})"
