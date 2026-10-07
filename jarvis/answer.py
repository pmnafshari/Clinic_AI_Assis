"""One exchange (J02): the heard request -> local speech to text -> the clinic guides over loopback -> the answer on the
Jarvis page, with its citation. Nothing is spoken back (J04 is BLOCKED by POL-13). The transcript and the answer live in
memory only, on the page for ANSWER_SECONDS; the state's reason, which the history keeps, never carries what was said.
"""
import time

from jarvis.clinic import LinkDown, LinkRefused
from jarvis.stt import Unclear

ANSWER_SECONDS = 120


class Exchange:
    def __init__(self, machine, transcribe, link_factory, clock=time.monotonic):
        self.machine, self.transcribe, self.link_factory, self.clock = machine, transcribe, link_factory, clock

    def show(self, answer):
        self.machine.show(answer, ANSWER_SECONDS, clock=self.clock)

    def __call__(self, pcm):
        """-> the reason for going back to READY."""
        self.machine.go("ACTIVE", "working out what was asked (on this computer)")
        try:
            heard, _language = self.transcribe(pcm)
        except Unclear as e:
            self.show({"outcome": "unclear", "heard": None, "message": f"I did not catch that: {e}. Nothing was asked.",
                       "citations": [], "warnings": []})
            return "did not catch the question - nothing was asked"
        link = self.link_factory()
        if link is None:
            return self.unavailable(heard, "this computer has no Jarvis device credential (Admin > Jarvis devices)")
        self.machine.go("ACTIVE", "asking the approved clinic guides")
        try:
            result = link.ask_guides(heard)
        except (LinkDown, LinkRefused) as e:
            return self.unavailable(heard, str(e))
        self.show({**result, "heard": heard})
        if result["outcome"] == "answer":
            c = result["citations"][0]
            return f"answered on screen ({c['title']}, page {c['page']})"
        return f"a refusal is on screen ({result['reason']})"

    def unavailable(self, heard, why):
        self.show({"outcome": "unavailable", "heard": heard, "message": f"The clinic guides could not be asked: {why}.",
                   "citations": [], "warnings": []})
        return f"could not ask the clinic guides ({why})"
