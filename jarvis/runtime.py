"""The clinic link check. Since J01 it no longer decides the state: READY comes only from real listening
(jarvis/listen.py); the link is shown beside it, because guide answers (J02) and protected requests (J03) need it."""
from jarvis.clinic import LinkDown, LinkRefused


def check_link(machine, link):
    if link is None:
        return machine.set_clinic(False, "no device credential on this machine")
    try:
        who = link.whoami()
    except (LinkRefused, LinkDown) as e:
        return machine.set_clinic(False, str(e))
    acting = who.get("delegation")
    detail = f"connected as {who.get('device')}" + (f"; acting for {acting['username']} ({acting['role']})" if acting else
                                                     "; no staff session delegated")
    machine.set_clinic(True, detail)
