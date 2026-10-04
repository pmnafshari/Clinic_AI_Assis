"""The macOS LaunchAgent that starts Jarvis at login and restarts it after a crash. Installing it is the owner's (J-D4)."""
import plistlib

LABEL = "com.clinicdemo.jarvis"
INDICATOR_LABEL = "com.clinicdemo.jarvis-indicator"


def plist(python, workdir, log_dir):
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [python, f"{workdir}/jarvis_run.py"],
        "WorkingDirectory": workdir,
        "RunAtLoad": True,
        # restart after a crash or a non-zero exit, not after a clean stop
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "ProcessType": "Interactive",
        # service events only: no audio, no transcripts are ever written
        "StandardOutPath": f"{log_dir}/clinic-demo-jarvis.log",
        "StandardErrorPath": f"{log_dir}/clinic-demo-jarvis.log",
    })


def indicator_plist(python, workdir, log_dir):
    return plistlib.dumps({
        "Label": INDICATOR_LABEL,
        "ProgramArguments": [python, f"{workdir}/jarvis_indicator.py"],
        "WorkingDirectory": workdir,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "ProcessType": "Interactive",
        "LimitLoadToSessionType": "Aqua",
        "StandardOutPath": f"{log_dir}/clinic-demo-jarvis-indicator.log",
        "StandardErrorPath": f"{log_dir}/clinic-demo-jarvis-indicator.log",
    })


def install(python, workdir, log_dir, agents_dir, run, uid):
    """Write both login items in the user's LaunchAgents folder and load them (reversible: uninstall)."""
    from pathlib import Path
    agents = Path(agents_dir)
    agents.mkdir(parents=True, exist_ok=True)
    for label, body in ((LABEL, plist(python, workdir, log_dir)), (INDICATOR_LABEL, indicator_plist(python, workdir, log_dir))):
        path = agents / f"{label}.plist"
        path.write_bytes(body)
        run(["launchctl", "bootstrap", f"gui/{uid}", str(path)])


def uninstall(agents_dir, run, uid):
    from pathlib import Path
    for label in (LABEL, INDICATOR_LABEL):
        path = Path(agents_dir) / f"{label}.plist"
        run(["launchctl", "bootout", f"gui/{uid}", str(path)])
        if path.exists():
            path.unlink()
