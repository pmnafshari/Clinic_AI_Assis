"""One exchange (J02): the heard request -> local speech to text -> the clinic guides over loopback -> the answer on the
Jarvis page, with its citation (read aloud only in the synthetic demo - jarvis/speech.py). The transcript and the answer live in
memory only, on the page for ANSWER_SECONDS; the state's reason, which the history keeps, never carries what was said.

Since J-D10 the speech to text is hinted with the library's own terms, and what was heard is shown first: nothing is
asked until someone presses "Yes, ask this" on the page. No, a timeout or anything ambiguous discards it unasked.

Since J02 follow-up 6 an exchange is one numbered interaction running beside the listener: every write is made only
while that interaction is current (a new wake, DEGRADED or a refused device ends it), the device and its delegation are
checked again before what was heard is shown, every few seconds while it waits, and before it is asked; each kind of
failure has its own card; and answers are given only when switched on (JARVIS_GUIDE_ANSWERS=demo, jarvis_run.py).
"""
from jarvis import notes, patients
from jarvis.clinic import LinkDown, LinkRefused, NoteRefused
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
NOTE_TITLES = {             # J03: why a note was not saved, as the card's title
    "auth_required": "Sign-in needed", "role": "Not for this role", "empty": "No note was heard",
    "no_match": "No patient by that name", "too_many": "Several patients match", "patient": "Not one of the patients",
    "gone": "This note is no longer waiting", "unclear": "Say the note again",
    "which": "Which patient?", "field": "Not this field", "value": "Not a phone number",
    "changed": "The record changed",
}
HANDLED_SECONDS = 300       # "her next appointment": the patient just handled, for this long, for the same doctor
SAID = 'Say "show me", "next appointment for" or "change the phone number of" and the patient\'s full name.'
NOTE_SAYING = 'Say: "add a note to" the patient\'s full name, then "saying" and the note.'
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
                 recheck_seconds=RECHECK_SECONDS, confirm=True, notes=False):
        self.machine, self.transcribe, self.link_factory = machine, transcribe, link_factory
        self.enabled, self.confirm_seconds, self.recheck_seconds = enabled, confirm_seconds, recheck_seconds
        self.confirm = confirm      # False only for the synthetic demo (owner decision 2026-10-11): asked at once
        self.notes = notes          # notes, reads and phone changes by voice (J03), only in the synthetic demo
        self.handled = None         # the patient just handled: {cf, name, user, at} - memory only

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
        command = notes.parse(heard) if self.notes else None
        if command is not None:
            return self.note(turn, link, given, heard, *command)
        asked = patients.parse(heard) if self.notes else None
        if asked is not None:
            out = self.patient(turn, link, given, heard, asked)
            if out is not None:
                return out
            if not m.current(turn):
                return "cancelled"
        decision = "ask"
        if self.confirm:
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
        return f"a refusal is on screen ({result.get('title') or result['reason']})"

    def note(self, turn, link, given, heard, patient, words):
        """A note by voice (J03): drafted at the clinic, put on screen with the patients the name matches, saved only
        when someone clicks one of them. The clinic decides everything; the reasons kept name nobody."""
        m = self.machine
        if patient is None:
            return self.note_card(turn, heard, "unclear", NOTE_SAYING, "a note command could not be read")
        if given[1] is None:                       # no staff session delegated: the clinic is not even asked
            return self.note_card(turn, heard, "auth_required",
                                  "A doctor must sign Jarvis in first, on this page. Nothing was written.",
                                  "a note needs a delegated staff session - nothing written")
        if not m.go_for(turn, "ACTIVE", "preparing the note - not recording"):
            return "cancelled"
        try:
            draft = link.note_draft(patient, words)
        except NoteRefused as e:
            return self.note_card(turn, heard, e.code, f"{e} Nothing was written.", f"note not drafted ({e.code})")
        except LinkRefused as e:
            return self.problem(turn, "refused", str(e), heard)
        except LinkDown as e:
            return self.problem(turn, "clinic_down", str(e), heard)
        pid = m.offer_note(turn, {"heard": heard, "patient_said": patient, "text": draft["text"],
                                  "choices": draft["choices"], "citations": [], "warnings": []}, self.confirm_seconds * 2)
        if pid is None:
            return "cancelled"
        decision = self.wait(turn, pid, link, given)
        if decision == "cancelled":
            return "cancelled"
        if not decision.startswith("save:"):
            message = {"discard": "You pressed Discard. Nothing was saved.",
                       "timeout": "Not saved in time. Nothing was saved."}.get(
                decision, "The choice was not clear. Nothing was saved.")
            m.show_for(turn, {"outcome": "note_discarded", "title": "Not saved", "message": message, "heard": heard,
                              "citations": [], "warnings": []}, ANSWER_SECONDS)
            return "the note was not saved"
        self.recheck(turn, link, given)
        try:
            out = link.note_commit(draft["draft"], decision[len("save:"):])
        except NoteRefused as e:
            return self.note_card(turn, heard, e.code, f"{e} Nothing was saved.", f"note not saved ({e.code})")
        except LinkRefused as e:
            return self.problem(turn, "refused", str(e), heard)
        except LinkDown as e:
            return self.problem(turn, "clinic_down", str(e), heard)
        self.remember(given, out["cf"], out["name"])
        m.show_for(turn, {"outcome": "note_saved", "title": "Note saved", "private": True, "say": "Note saved.",
                          "message": f"Note saved to {out['name']}'s record.", "heard": heard,
                          "patient": {"cf": out["cf"], "name": out["name"]}, "citations": [], "warnings": []},
                   ANSWER_SECONDS)
        return "a note was saved after it was picked on screen"

    # --- reads and phone changes by voice (J03 slice 2) - what is shown about a patient is never spoken -------------
    def remember(self, given, cf, name):
        self.handled = {"cf": cf, "name": name, "user": given[1], "at": self.machine.clock(),
                        "epoch": self.machine.access_epoch}

    def just_handled(self, given):
        h = self.handled
        if h is None or h["user"] != given[1] or h["epoch"] != self.machine.access_epoch \
                or self.machine.clock() - h["at"] > HANDLED_SECONDS:
            self.handled = None
            return None
        return h

    def patient(self, turn, link, given, heard, asked):
        """Open a record, read a next appointment or change a phone number by voice. -> the reason; None when a bare
        "show me X" turns out not to be about a patient (it is then a clinic-guide question)."""
        m = self.machine
        bare = asked["kind"] == "open" and not asked["explicit"]
        if given[1] is None:                       # nobody signed in: the clinic is not asked
            if bare:
                return None
            return self.note_card(turn, heard, "auth_required",
                                  "A doctor must sign Jarvis in first, on this page. Nothing was read or changed.",
                                  "a patient request needs a doctor signed in - nothing read")
        cf, name = None, asked["name"]
        if name is None:                           # her / his / their
            h = self.just_handled(given)
            if h is None:
                return self.note_card(turn, heard, "which", "Say the patient's full name. " + SAID,
                                      "no patient handled just now - nothing read")
            cf, name = h["cf"], h["name"]
        if not m.go_for(turn, "ACTIVE", "looking up the patient - not recording"):
            return "cancelled"
        try:
            if asked["kind"] == "edit":
                return self.edit(turn, link, given, heard, name, cf, asked)
            found = link.read_patients(name, asked["kind"], cf)
        except NoteRefused as e:
            if bare and e.code == "no_match":
                return None
            return self.note_card(turn, heard, e.code, f"{e} Nothing was read or changed.",
                                  f"patient request refused ({e.code})")
        except LinkRefused as e:
            return self.problem(turn, "refused", str(e), heard)
        except LinkDown as e:
            return self.problem(turn, "clinic_down", str(e), heard)
        choices = found["choices"]
        if asked["kind"] == "next":
            if len(choices) == 1:
                self.remember(given, choices[0]["cf"], choices[0]["name"])
            m.show_for(turn, {"outcome": "patient_next", "title": "Next appointment", "private": True,
                              "say": "The answer is on screen.", "heard": heard, "choices": choices,
                              "citations": [], "warnings": []}, ANSWER_SECONDS)
            return "a next appointment is on screen"
        if len(choices) == 1:
            c = choices[0]
            self.remember(given, c["cf"], c["name"])
            m.show_for(turn, {"outcome": "patient_open", "title": "Patient record", "private": True,
                              "say": "It is on screen.", "heard": heard, "choices": choices, "open": c["cf"],
                              "citations": [], "warnings": []}, ANSWER_SECONDS)
            return "a record was opened on screen"
        pid = m.offer_note(turn, {"heard": heard, "title": "Which patient?", "private": True,
                                  "say": "Pick the patient on screen.", "choices": choices, "citations": [],
                                  "warnings": []}, self.confirm_seconds * 2, outcome="patient_pick", verb="open",
                           reason="patients are on screen, waiting for one to be opened")
        if pid is None:
            return "cancelled"
        decision = self.wait(turn, pid, link, given)
        if decision == "cancelled":
            return "cancelled"
        if not decision.startswith("open:"):
            m.show_for(turn, {"outcome": "note_discarded", "title": "Not opened", "message": "Nothing was opened.",
                              "heard": heard, "citations": [], "warnings": []}, ANSWER_SECONDS)
            return "no record was picked"
        c = next(x for x in choices if x["cf"] == decision[len("open:"):])
        self.remember(given, c["cf"], c["name"])
        m.show_for(turn, {"outcome": "patient_open", "title": "Patient record", "private": True, "say": "It is open.",
                          "heard": heard, "choices": [c], "citations": [], "warnings": []}, ANSWER_SECONDS)
        return "a record was opened after it was picked on screen"

    def edit(self, turn, link, given, heard, name, cf, asked):
        m = self.machine
        draft = link.edit_draft(name, asked["field"], asked["value"], cf)
        pid = m.offer_note(turn, {"heard": heard, "title": "Change the phone number?", "private": True,
                                  "say": "Check the change on screen.", "field": draft["field"],
                                  "value": draft["value"], "choices": draft["choices"], "citations": [],
                                  "warnings": []}, self.confirm_seconds * 2, outcome="edit_confirm",
                           reason="a change is on screen, waiting to be saved there")
        if pid is None:
            return "cancelled"
        decision = self.wait(turn, pid, link, given)
        if decision == "cancelled":
            return "cancelled"
        if not decision.startswith("save:"):
            message = {"discard": "You pressed Discard. Nothing was changed.",
                       "timeout": "Not saved in time. Nothing was changed."}.get(
                decision, "The choice was not clear. Nothing was changed.")
            m.show_for(turn, {"outcome": "note_discarded", "title": "Not changed", "message": message, "heard": heard,
                              "citations": [], "warnings": []}, ANSWER_SECONDS)
            return "the change was not saved"
        self.recheck(turn, link, given)
        try:
            out = link.edit_commit(draft["draft"], decision[len("save:"):])
        except NoteRefused as e:
            return self.note_card(turn, heard, e.code, f"{e} Nothing was changed.", f"change not saved ({e.code})")
        self.remember(given, out["cf"], out["name"])
        m.show_for(turn, {"outcome": "edit_saved", "title": "Phone number saved", "private": True, "say": "Saved.",
                          "message": f"{out['name']}'s phone number is now {out['value']}.", "heard": heard,
                          "choices": [{"cf": out["cf"], "name": out["name"]}], "citations": [], "warnings": []},
                   ANSWER_SECONDS)
        return "a phone number was changed after it was picked on screen"

    def note_card(self, turn, heard, code, message, reason):
        self.machine.show_for(turn, {"outcome": "refused", "reason": code, "title": NOTE_TITLES.get(code, "Not saved"),
                                     "message": message, "heard": heard, "citations": [], "warnings": []},
                              ANSWER_SECONDS)
        return reason

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
