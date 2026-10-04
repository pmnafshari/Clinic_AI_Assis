"""The macOS LaunchAgent that starts Jarvis at login and restarts it after a crash. Installing it is the owner's (J-D4)."""
import plistlib

LABEL = "com.clinicdemo.jarvis"


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
