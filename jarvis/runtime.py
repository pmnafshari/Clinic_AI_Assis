"""The clinic link check. Since J01 it no longer decides the state: READY comes only from real listening
(jarvis/listen.py); the link is shown beside it, because guide answers (J02) and protected requests (J03) need it.
Since J02 follow-up 6 it also says whether clinic-guide answers can be given here: switched on (only for the synthetic
demo walkthrough) and set up - shown apart from listening."""
from jarvis.clinic import LinkDown, LinkRefused

OFF = "switched off on this computer (J02 is not accepted for use); listening is not affected"
DEMO = "on - SYNTHETIC DEMO MODE (J02 is not accepted; synthetic library only)"
DEMO_BUT = "switched on for the synthetic demo, but not available: "


def check_link(machine, link, answers_on=False):
    if link is None:
        machine.set_clinic(False, "no device credential on this machine")
        return answers(machine, answers_on, "no device credential (Admin > Jarvis devices)")
    try:
        who = link.whoami()
    except LinkRefused as e:
        machine.set_clinic(False, str(e))
        return answers(machine, answers_on, "the clinic app refused this computer (not registered or revoked)")
    except LinkDown as e:
        machine.set_clinic(False, str(e))
        return answers(machine, answers_on, "the clinic app is unreachable")
    acting = who.get("delegation")
    detail = f"connected as {who.get('device')}" + (f"; acting for {acting['username']} ({acting['role']})" if acting else
                                                     "; no staff session delegated")
    machine.set_clinic(True, detail)
    answers(machine, answers_on, None)


def answers(machine, on, missing):
    if not on:
        machine.set_answers(False, OFF)
    elif missing:
        machine.set_answers(False, DEMO_BUT + missing)
    else:
        machine.set_answers(True, DEMO)
