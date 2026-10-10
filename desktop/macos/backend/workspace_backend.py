"""The isolated demo's clinic backend inside Clinic Workspace.app (P28): the clinic's own startup (run.main) on the
port the app gives it, in the data folder the app sets as the working directory - inside the app's sandbox container,
which this process inherits, so the clinic's real database is not even readable from here.

    python workspace_backend.py PORT          (started by the app; working directory = the demo's data folder)

It refuses to run in the clinic's own folder, without the demo profile, or on a port the clinic or its other apps use.
"""
import os
import sys
from pathlib import Path

CLINIC_PORTS = {5000, 5001, 5002, 5020}       # staff, patient portal, public site, Jarvis


def refuse(why):
    print(f"workspace_backend: refused - {why}", file=sys.stderr)
    sys.exit(2)


def main(argv):
    if os.environ.get("CLINIC_WORKSPACE_PROFILE") != "demo":
        refuse("only the demo profile starts a backend here")
    if (Path.cwd() / "run.py").exists() or (Path.cwd() / ".venv").exists():
        refuse("the working directory is the clinic's own folder - the demo has its own data folder")
    port = int(argv[0]) if argv and argv[0].isdigit() else 0
    if port < 1024 or port in CLINIC_PORTS:
        refuse(f"port {port} belongs to the clinic or is not a valid port")
    import run
    run.main(port=port)


if __name__ == "__main__":
    main(sys.argv[1:])
