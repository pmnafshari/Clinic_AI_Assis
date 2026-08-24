"""refuse to start when patient data at rest is not encrypted.

D-14 control 3 (arch doc 5.3) forbids real patient data until storage is
encrypted. filevault already covers the whole surface - clinic.sqlite, chroma,
sorted/, log.txt, undo_log.jsonl - so nothing here encrypts anything. what was
missing was any guarantee it is still on when the apps run.

sibling of tunnel_guard.py: pure core over an input string, thin wrapper that
does the i/o and exits, selftest covering every branch.
"""

import os
import subprocess
import sys

DISARM_ENV_VAR = "DISK_GUARD_DISARMED"
OFF_VALUES = frozenset({"", "0", "false", "no", "off"})

# fdesetup is macos-only. a missing binary means we cannot prove encryption is
# on, which is handled as "unknown" -> refuse, not as "probably fine".
FDESETUP = "/usr/bin/fdesetup"


def filevault_state(status_output):
    # pure: takes fdesetup's stdout, never runs it. that is what makes the
    # refusal branches assertable - nobody is turning filevault off to run a
    # selftest.
    if not status_output:
        return "unknown"
    text = " ".join(status_output.split()).lower()
    # match the meaningful token, not the whole sentence. apple has reworded
    # this string across releases and an equality check would refuse to boot a
    # perfectly encrypted machine, which is its own kind of outage.
    if "filevault is on" in text:
        return "on"
    if "filevault is off" in text:
        return "off"
    return "unknown"


def disarmed():
    # default is ARMED: unset means the guard runs. written this way round so a
    # typo cannot silently disable it.
    value = os.environ.get(DISARM_ENV_VAR)
    if value is None:
        return False
    return value.strip().lower() not in OFF_VALUES


def read_status():
    # the only function that shells out. kept tiny so the pure core stays the
    # thing under test.
    try:
        done = subprocess.run(
            [FDESETUP, "status"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout


def guard_or_exit():
    if disarmed():
        print(
            f"*** DISK GUARD DISARMED: {DISARM_ENV_VAR} is set, so full-disk "
            f"encryption is NOT being checked. patient data at rest may be "
            f"readable by anyone who takes this disk. unset it on the clinic "
            f"machine. ***",
            file=sys.stderr,
        )
        return

    output = read_status()
    if output is None:
        _refuse(
            f"could not run {FDESETUP} - cannot prove patient data is encrypted "
            f"at rest. on a non-macos host set {DISARM_ENV_VAR} deliberately."
        )

    state = filevault_state(output)
    if state == "off":
        _refuse(
            "FileVault is OFF. D-14 control 3 (arch doc 5.3) forbids running "
            "against real patient data on an unencrypted disk."
        )
    if state == "unknown":
        _refuse(
            f"could not understand {FDESETUP} output, so encryption cannot be "
            f"proven. got: {output.strip()!r}"
        )

    print("disk guard: FileVault on")


def _refuse(message):
    print("DISK GUARD REFUSED TO START:", file=sys.stderr)
    print(f"  {message}", file=sys.stderr)
    sys.exit(1)


def selftest():
    # 1. the three recognised states
    assert filevault_state("FileVault is On.") == "on", "1: 'On.' should read as on"
    assert filevault_state("FileVault is Off.") == "off", "1: 'Off.' should read as off"

    # 2. case and whitespace must not decide whether a clinic can boot
    assert filevault_state("filevault is on") == "on", "2: match must be case-insensitive"
    assert filevault_state("  FileVault   is   On.  \n") == "on", "2: whitespace must not matter"
    assert filevault_state("FileVault is On (Deferred).") == "on", "2: trailing detail must not break the match"

    # 3. FAIL CLOSED. anything unrecognised is unknown, never a guess. this is
    # the assertion that matters most in this file: a wrong answer here is the
    # difference between refusing and silently running unencrypted.
    assert filevault_state("") == "unknown", "3: empty output must be unknown, not on"
    assert filevault_state(None) == "unknown", "3: no output must be unknown, not on"
    assert filevault_state("Encryption status: enabled") == "unknown", \
        "3: an unrecognised reword must be unknown, not on"
    assert filevault_state("command not found") == "unknown", \
        "3: an error string must be unknown, not on"

    # 4. the disarm flag defaults to ARMED
    saved = os.environ.pop(DISARM_ENV_VAR, None)
    try:
        assert disarmed() is False, "4: unset must mean armed"
        for off in OFF_VALUES:
            os.environ[DISARM_ENV_VAR] = off
            assert disarmed() is False, f"4: {off!r} must not disarm"
        for on in ("1", "true", "yes", "anything"):
            os.environ[DISARM_ENV_VAR] = on
            assert disarmed() is True, f"4: {on!r} should disarm"
        os.environ[DISARM_ENV_VAR] = " TRUE "
        assert disarmed() is True, "4: surrounding space and case must not matter"
    finally:
        if saved is None:
            os.environ.pop(DISARM_ENV_VAR, None)
        else:
            os.environ[DISARM_ENV_VAR] = saved

    assert os.environ.get(DISARM_ENV_VAR) == saved, "4: the flag must not leak out of section 4"

    # 5. leaf module - it must not reach into the app. checked over the AST's
    # import nodes, NOT by searching the source text: a whole-file grep matches
    # this very assertion's own strings and fails on a correct file. that exact
    # trap was recorded against phase 19 and hit again here while writing this.
    import ast

    forbidden = {"app", "run", "storage", "web_auth", "web_session"}
    imported = set()
    for node in ast.walk(ast.parse(open(__file__).read())):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[0])

    leaked = imported & forbidden
    assert not leaked, f"5: disk_guard must not import {sorted(leaked)}"
    assert "os" in imported, "5: the AST scan must actually see this module's imports"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python disk_guard.py --selftest")
    sys.exit(1)


if __name__ == "__main__":
    main()
