"""Start-up and periodic checks. Honest by construction: never READY until listening exists (J01)."""
from jarvis.clinic import LinkDown, LinkRefused

LISTENING_BUILT = False   # J01 turns this on with a real wake-phrase engine; until then READY would be a false claim


def check(machine, link):
    """One pass: link, device, listening. Moves the machine to the true state with the reason."""
    if machine.state not in ("STARTING", "DEGRADED"):
        return
    if machine.state == "DEGRADED":
        machine.go("STARTING", "checking again")
    if link is None:
        return machine.go("DEGRADED", "no device credential on this machine")
    try:
        link.whoami()
    except LinkRefused as e:
        return machine.go("DEGRADED", str(e))
    except LinkDown as e:
        return machine.go("DEGRADED", str(e))
    if not LISTENING_BUILT:
        return machine.go("DEGRADED", "listening is not built yet (J01)")
    machine.go("READY", "listening for the wake phrase")
