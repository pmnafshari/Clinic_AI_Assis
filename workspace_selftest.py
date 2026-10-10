"""P28 (MACOS-APP): the clinic side of the Clinic Workspace app. The app itself is tested installed (its own selftest
hook, `desktop/macos/`); this file covers what the clinic backend gives it and how its demo backend starts.

    .venv/bin/python workspace_selftest.py

1. /api/session: who is signed in and what the clinic lets that role do - for the app's permission centre; signed-in
   staff only, read-only, nothing else in it, never cached
2. run.py can be imported without starting a server, and run.main(port) is the one startup both backends use
3. the demo backend (desktop/macos/backend/workspace_backend.py) refuses to run in the clinic's own folder or without
   the demo profile - it can never open the clinic's real database
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import app.db as app_db
import auth
from patient_files_selftest import staff_client

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "desktop" / "macos" / "backend" / "workspace_backend.py"


def session_endpoint(tmp):
    # 1.
    db = str(Path(tmp) / "clinic.sqlite")
    app_db.DB_PATH = db
    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    anon = app.test_client().get("/api/session")
    assert anon.status_code in (302, 303) and "/login" in anon.headers.get("Location", ""), \
        f"1: HARD FAIL - /api/session answered without a sign-in ({anon.status_code})"
    assert auth.ROUTE_POLICY.get("auth.session") == "login", "1: the route is in the policy, for signed-in staff"
    for user, role in (("aassist", "assistant"), ("drossi", "dentist"), ("anadmin", "admin")):
        r = staff_client(app, db, user, role).get("/api/session")
        body = r.get_json()
        assert r.status_code == 200 and set(body) == {"username", "role", "capabilities"}, f"1: {role}: {r.status_code} {body}"
        assert body["username"] == user and body["role"] == role, f"1: {role}: the session's own user ({body})"
        assert body["capabilities"] == sorted(auth.PERMISSIONS[role]), f"1: {role}: the clinic's own capabilities"
        assert "no-store" in r.headers.get("Cache-Control", ""), "1: never cached"
    admin = staff_client(app, db, "anadmin", "admin").get("/api/session").get_json()
    assert admin["capabilities"] == ["manage_users"], "1: admin stays limited to administration"
    assert staff_client(app, db, "aassist", "assistant").post("/api/session").status_code in (400, 405), "1: read only"


def run_imports_quietly():
    # 2.
    code = "import run, inspect; print('port' in inspect.signature(run.main).parameters)"
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip().endswith("True"), \
        f"2: importing run.py must not start the clinic app ({r.returncode} {r.stdout[-200:]} {r.stderr[-300:]})"


def demo_backend_refuses():
    # 3.
    assert BACKEND.is_file(), "3: the demo backend launcher exists"
    env = dict(os.environ, CLINIC_WORKSPACE_PROFILE="demo", PYTHONPATH=str(ROOT))
    r = subprocess.run([sys.executable, str(BACKEND), "5100"], cwd=ROOT, env=env, capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 2 and "clinic's own folder" in r.stderr, \
        f"3: HARD FAIL - the demo backend ran in the clinic folder ({r.returncode} {r.stderr[-300:]})"
    with tempfile.TemporaryDirectory() as tmp:
        env.pop("CLINIC_WORKSPACE_PROFILE")
        r = subprocess.run([sys.executable, str(BACKEND), "5100"], cwd=tmp, env=env, capture_output=True, text=True,
                           timeout=60)
        assert r.returncode == 2 and "demo profile" in r.stderr, f"3: only the demo profile ({r.returncode} {r.stderr[-300:]})"
        env["CLINIC_WORKSPACE_PROFILE"] = "demo"
        r = subprocess.run([sys.executable, str(BACKEND), "5000"], cwd=tmp, env=env, capture_output=True, text=True,
                           timeout=60)
        assert r.returncode == 2 and "5000" in r.stderr, "3: never the clinic's own port"


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        session_endpoint(tmp)
    run_imports_quietly()
    demo_backend_refuses()
    print(json.dumps({"workspace_selftest": "ok"}))


if __name__ == "__main__":
    selftest()
    sys.exit(0)
