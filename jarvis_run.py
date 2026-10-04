"""Jarvis, the clinic's local voice companion - the background service (J00).

    .venv/bin/python jarvis_run.py                 run the service: page and status on http://127.0.0.1:5020
    .venv/bin/python jarvis_run.py --store-token   save this device's credential (from Admin > Jarvis devices), 600
    .venv/bin/python jarvis_run.py --agent-plist   print the LaunchAgent that starts Jarvis at login (install: J-D4)

    JARVIS_CLINIC_URL   the clinic staff app (default http://127.0.0.1:5000)
"""
import getpass
import logging
import os
import signal
import sys
import threading
from pathlib import Path

from jarvis import launchd, runtime, states
from jarvis.clinic import DEFAULT_KEY, ClinicLink, store_key
from jarvis.web import PORT, create_app

CHECK_SECONDS = 30


def _link():
    try:
        return ClinicLink(os.environ.get("JARVIS_CLINIC_URL", "http://127.0.0.1:5000"), DEFAULT_KEY)
    except FileNotFoundError:
        return None


def serve():
    from werkzeug.serving import make_server
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    machine = states.Machine()
    stop = threading.Event()

    def checks():
        while not stop.is_set():
            try:
                runtime.check(machine, _link())
            except Exception as e:  # a fault is a state with its reason, never a silent stop
                if machine.state != "DEGRADED":
                    machine.go("DEGRADED", f"service fault ({type(e).__name__})")
            stop.wait(CHECK_SECONDS)
    threading.Thread(target=checks, name="jarvis-checks", daemon=True).start()
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
    if argv:
        print(__doc__)
        return 2
    return serve()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
