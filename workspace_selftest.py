"""P28 (MACOS-APP): the clinic side of the Clinic Workspace app. The app itself is tested installed (its own selftest
hook, `desktop/macos/`); this file covers what the clinic backend gives it and how its demo backend starts.

    .venv/bin/python workspace_selftest.py

1. /api/session: who is signed in and what the clinic lets that role do - for the app's permission centre; signed-in
   staff only, read-only, nothing else in it, never cached
2. run.py can be imported without starting a server, and run.main(port) is the one startup both backends use
3. the demo backend (desktop/macos/backend/workspace_backend.py) refuses to run in the clinic's own folder or without
   the demo profile - it can never open the clinic's real database
4. the demo backend ends itself when the app that started it is gone (a crash or a force-quit leaves no backend
   holding the demo's port)
5. inside the App Sandbox the backend reads no host configuration file: Python's MIME table is its built-in one (the
   installed app's first start crashed reading /etc/apache2/mime.types, which the sandbox refuses)
6. the disk guard (D-14 control 3) is disarmed for the synthetic demo only - fdesetup reports FileVault Off inside the
   sandbox although it is On (decision D-P28-1); the clinic's own run.py keeps it armed
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


def follows_its_parent():
    # 4.
    sys.path.insert(0, str(BACKEND.parent))
    import workspace_backend as wb
    ppids = iter([111, 111, 1])
    ended = []
    wb.watch_parent(111, getppid=lambda: next(ppids), end=lambda: ended.append(True), interval=0)
    assert ended == [True], "4: HARD FAIL - the backend outlived the app"
    code = ("import sys, subprocess, time; sys.path.insert(0, %r); import workspace_backend as wb;"
            " import os, threading; p = os.getppid(); threading.Thread(target=wb.watch_parent, args=(p,),"
            " kwargs={'interval': 0.1}, daemon=True).start(); time.sleep(30)") % str(BACKEND.parent)
    parent = subprocess.Popen([sys.executable, "-c", f"import subprocess, sys; subprocess.Popen([sys.executable, '-c', {code!r}]); import time; time.sleep(0.5)"])
    parent.wait(10)
    import time
    deadline = time.monotonic() + 5
    leftover = True
    while time.monotonic() < deadline:
        r = subprocess.run(["pgrep", "-f", "watch_parent, args"], capture_output=True, text=True)
        if not r.stdout.strip():
            leftover = False
            break
        time.sleep(0.2)
    assert not leftover, "4: HARD FAIL - a real child kept running after its parent ended"


def no_host_files():
    # 5.
    code = ("import sys; sys.path.insert(0, %r); import workspace_backend, mimetypes; "
            "print(mimetypes.knownfiles == [] and mimetypes.guess_type('a.pdf')[0])") % str(BACKEND.parent)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert r.stdout.strip() == "application/pdf", f"5: the backend must not read host MIME files ({r.stdout} {r.stderr[-200:]})"


def guard_only_for_demo():
    # 6.
    src = BACKEND.read_text()
    assert 'os.environ["DISK_GUARD_DISARMED"] = "1"' in src, "6: the demo launcher disarms the guard itself"
    i = src.index("def main(argv):")
    body = src[i:]
    assert body.index('"CLINIC_WORKSPACE_PROFILE") != "demo"') < body.index("DISK_GUARD_DISARMED"), \
        "6: HARD FAIL - disarmed before the demo profile is checked"
    run_src = (ROOT / "run.py").read_text()
    assert "disk_guard.guard_or_exit()" in run_src and "DISK_GUARD_DISARMED" not in run_src, \
        "6: HARD FAIL - the clinic's own startup must keep the guard armed"


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        session_endpoint(tmp)
    run_imports_quietly()
    demo_backend_refuses()
    follows_its_parent()
    no_host_files()
    guard_only_for_demo()
    print(json.dumps({"workspace_selftest": "ok"}))


if __name__ == "__main__":
    selftest()
    sys.exit(0)
