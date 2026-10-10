"""The isolated demo's clinic backend inside Clinic Workspace.app (P28): the clinic's own startup (run.main) on the
port the app gives it, in the data folder the app sets as the working directory - inside the app's sandbox container,
which this process inherits, so the clinic's real database is not even readable from here.

    python workspace_backend.py PORT          (started by the app; working directory = the demo's data folder)

It refuses to run in the clinic's own folder, without the demo profile, or on a port the clinic or its other apps use.
"""
import mimetypes
import os
import sys
import threading
import time
from pathlib import Path

# inside the App Sandbox a host file such as /etc/apache2/mime.types is visible but not readable, and Python's
# mimetypes does not expect that (the installed app's first demo start crashed on it): use the built-in table only
mimetypes.knownfiles = []

CLINIC_PORTS = {5000, 5001, 5002, 5020}       # staff, patient portal, public site, Jarvis


def refuse(why):
    print(f"workspace_backend: refused - {why}", file=sys.stderr)
    sys.exit(2)


def watch_parent(parent, getppid=os.getppid, end=lambda: os._exit(0), interval=1.0):
    """Ends this backend once the app that started it is gone (reparented): a crash or a force-quit of the app must
    not leave a backend holding the demo's port and data."""
    while getppid() == parent:
        time.sleep(interval)
    end()


def main(argv):
    if os.environ.get("CLINIC_WORKSPACE_PROFILE") != "demo":
        refuse("only the demo profile starts a backend here")
    if (Path.cwd() / "run.py").exists() or (Path.cwd() / ".venv").exists():
        refuse("the working directory is the clinic's own folder - the demo has its own data folder")
    port = int(argv[0]) if argv and argv[0].isdigit() else 0
    if port < 1024 or port in CLINIC_PORTS:
        refuse(f"port {port} belongs to the clinic or is not a valid port")
    threading.Thread(target=watch_parent, args=(os.getppid(),), name="watch-app", daemon=True).start()
    import run
    run.main(port=port)


if __name__ == "__main__":
    main(sys.argv[1:])
