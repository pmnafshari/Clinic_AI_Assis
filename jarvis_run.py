"""Jarvis, the clinic's local voice companion - the background service (J00, listening since J01, clinic-guide
answers on its page since J02).

    .venv/bin/python jarvis_run.py                     run the service: page and status on http://127.0.0.1:5020
    .venv/bin/python jarvis_run.py --store-token       save this device's credential (from Admin > Jarvis devices), 600
    .venv/bin/python jarvis_run.py --agent-plist       print the service's LaunchAgent
    .venv/bin/python jarvis_run.py --install-agents    start Jarvis and its menu-bar indicator at login (this user only)
    .venv/bin/python jarvis_run.py --uninstall-agents  stop them and remove both login items

    JARVIS_CLINIC_URL     the clinic staff app (default http://127.0.0.1:5000)
    JARVIS_GUIDE_ANSWERS  "demo" switches clinic-guide answers on for the synthetic demo walkthrough (J02 follow-up 6);
                          anything else, or nothing, keeps them off - J02 is not accepted for use. The service's
                          LaunchAgent never sets it; a credential alone never turns answers on.
"""
import getpass
import logging
import os
import signal
import sys
import threading
from pathlib import Path

from jarvis import answer, launchd, listen, runtime, states, stt
from jarvis.clinic import DEFAULT_KEY, ClinicLink, store_key
from jarvis.web import PORT, create_app

CHECK_SECONDS = 30


def answers_on(env):
    return env.get("JARVIS_GUIDE_ANSWERS") == "demo"


def _link():
    try:
        return ClinicLink(os.environ.get("JARVIS_CLINIC_URL", "http://127.0.0.1:5000"), DEFAULT_KEY)
    except FileNotFoundError:
        return None


def _detector():
    from jarvis.features import Features
    from jarvis.wake import MODEL, Detector, WakeModel
    try:
        return Detector(WakeModel(MODEL), Features())
    except (OSError, KeyError, ValueError) as e:      # missing, damaged or not the expected network
        raise listen.EngineMissing(f"{type(e).__name__}: {e}") from None


def serve(make_source=listen.SoundDeviceSource, cue=listen.chime):
    """The service. `make_source` and `cue` are the microphone and the tone; an integration harness swaps them for
    injected audio and silence (labelled as such) - nothing else."""
    from werkzeug.serving import make_server
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    machine = states.Machine()
    stop = threading.Event()
    enabled = answers_on(os.environ)

    def checks():
        while not stop.is_set():
            try:
                runtime.check_link(machine, _link(), enabled)
            except Exception as e:
                machine.set_clinic(False, f"link check fault ({type(e).__name__})")
            stop.wait(CHECK_SECONDS)

    def listening():
        exchange = answer.Exchange(machine, stt.transcribe, _link, enabled=enabled)
        lst = listen.Listener(machine, _detector, make_source, cue=cue, on_request=exchange)
        try:
            lst.run(stop)
        except Exception as e:  # a fault is a state with its reason, then a crash so launchd restarts us
            if machine.state != "DEGRADED":
                machine.go("DEGRADED", f"service fault ({type(e).__name__})")
            logging.exception("listener fault")
            os._exit(1)
    threading.Thread(target=checks, name="jarvis-checks", daemon=True).start()
    threading.Thread(target=listening, name="jarvis-listen", daemon=True).start()
    server = make_server("127.0.0.1", PORT, create_app(machine), threaded=True)
    signal.signal(signal.SIGTERM, lambda *a: (stop.set(), threading.Thread(target=server.shutdown).start()))
    print(f"jarvis: page and status on http://127.0.0.1:{PORT}; clinic-guide answers "
          f"{'ON - synthetic demo mode' if enabled else 'off'}", flush=True)
    server.serve_forever()
    return 0


def main(argv):
    if argv[:1] == ["--store-token"]:
        store_key(DEFAULT_KEY, getpass.getpass("Device credential (not shown): "))
        print(f"stored in {DEFAULT_KEY} (600)")
        return 0
    if argv[:1] == ["--agent-plist"]:
        root = str(Path(__file__).resolve().parent)
        sys.stdout.write(launchd.plist(f"{root}/.venv/bin/python", root, str(Path("~/Library/Logs").expanduser())).decode())
        return 0
    if argv[:1] in (["--install-agents"], ["--uninstall-agents"]):
        import subprocess
        root = str(Path(__file__).resolve().parent)
        agents = Path("~/Library/LaunchAgents").expanduser()
        run = lambda cmd: print(" ".join(cmd), "->", subprocess.run(cmd, check=False).returncode)  # noqa: E731
        if argv[0] == "--install-agents":
            launchd.install(f"{root}/.venv/bin/python", root, str(Path("~/Library/Logs").expanduser()), agents, run, os.getuid())
        else:
            launchd.uninstall(agents, run, os.getuid())
        return 0
    if argv:
        print(__doc__)
        return 2
    return serve()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
