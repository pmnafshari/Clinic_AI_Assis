"""The clinic link check. Since J01 it no longer decides the state: READY comes only from real listening
(jarvis/listen.py); the link is shown beside it, because guide answers (J02) and protected requests (J03) need it.
Since J02 follow-up 6 it also says whether clinic-guide answers can be given here: switched on (only for the synthetic
demo walkthrough) and set up - shown apart from listening.

Since the J02 UI review each missing prerequisite says what to do next and who may do it, an empty guide library is
one of them, and a missing staff delegation is said not to matter for clinic-guide answers (Jarvis asks as reception)."""
from jarvis.clinic import LinkDown, LinkRefused

OFF = "switched off on this computer (J02 is not accepted for use); listening is not affected"
DEMO = "on - SYNTHETIC DEMO MODE (J02 is not accepted; synthetic library only)"
DEMO_BUT = "switched on for the synthetic demo, but not available: "
NOT_REGISTERED = ("this computer is not registered - an admin registers it under Admin > Jarvis devices, then its "
                  "credential is stored here (jarvis_run.py --store-token)")
REFUSED = ("the clinic app refused this computer (not registered or revoked) - an admin registers it again under "
           "Admin > Jarvis devices")
DOWN = "the clinic app is unreachable - start the clinic staff app on this computer"
NO_GUIDES = ("no approved clinic guides yet - a dentist, or the designated approver for administrative documents, "
             "approves them in the Guides library of the clinic app")
NO_DELEGATION = ("no staff session delegated (not needed for clinic-guide answers; a staff member delegates their own "
                 "session under Jarvis in the clinic sidebar)")


def check_link(machine, link, answers_on=False):
    if link is None:
        machine.set_clinic(False, "no device credential on this machine")
        return answers(machine, answers_on, NOT_REGISTERED)
    try:
        who = link.whoami()
    except LinkRefused as e:
        machine.set_clinic(False, str(e))
        return answers(machine, answers_on, REFUSED)
    except LinkDown as e:
        machine.set_clinic(False, str(e))
        return answers(machine, answers_on, DOWN)
    acting = who.get("delegation")
    detail = f"connected as {who.get('device')}; " + (f"acting for {acting['username']} ({acting['role']})" if acting
                                                      else NO_DELEGATION)
    machine.set_clinic(True, detail)
    answers(machine, answers_on, None if who.get("approved_guides") else NO_GUIDES)


def answers(machine, on, missing):
    if not on:
        machine.set_answers(False, OFF)
    elif missing:
        machine.set_answers(False, DEMO_BUT + missing, demo=True, missing=missing)
    else:
        machine.set_answers(True, DEMO, demo=True)
