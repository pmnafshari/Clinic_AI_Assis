"""Jarvis, the clinic's local voice companion - the background service (J00, listening since J01).

    .venv/bin/python jarvis_run.py                     run the service: page and status on http://127.0.0.1:5020
    .venv/bin/python jarvis_run.py --store-token       save this device's credential (from Admin > Jarvis devices), 600
    .venv/bin/python jarvis_run.py --agent-plist       print the service's LaunchAgent
    .venv/bin/python jarvis_run.py --install-agents    start Jarvis and its menu-bar indicator at login (this user only)
    .venv/bin/python jarvis_run.py --uninstall-agents  stop them and remove both login items

    JARVIS_CLINIC_URL   the clinic staff app (default http://127.0.0.1:5000)
"""
import getpass
import logging
import os
import signal
import sys
import threading
from pathlib import Path

from jarvis import launchd, listen, runtime, states
from jarvis.clinic import DEFAULT_KEY, ClinicLink, store_key
from jarvis.web import PORT, create_app

CHECK_SECONDS = 30


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


def serve():
    from werkzeug.serving import make_server
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    machine = states.Machine()
    stop = threading.Event()

    def checks():
        while not stop.is_set():
            try:
                runtime.check_link(machine, _link())
            except Exception as e:
                machine.set_clinic(False, f"link check fault ({type(e).__name__})")
            stop.wait(CHECK_SECONDS)

    def listening():
        lst = listen.Listener(machine, _detector, listen.SoundDeviceSource, cue=listen.chime)
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
    print(f"jarvis: page and status on http://127.0.0.1:{PORT}", flush=True)
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
