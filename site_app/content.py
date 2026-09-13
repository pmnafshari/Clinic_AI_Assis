"""Clinic content, read once from clinic.yaml.

UX-07: no name, figure, price or opening hour is written into a template. The
clinic owner changes this file, not Jinja. pyyaml is already a dependency of
the project, so this adds nothing to install.
"""

import os
from pathlib import Path
from urllib.parse import urlsplit

import yaml

import codice_fiscale

CONFIG_PATH = Path(__file__).resolve().parent / "clinic.yaml"

# every top-level key a template depends on. checked at load so a truncated
# or half-edited file refuses to boot - a site that starts with its doctors
# section silently missing is worse than one that will not start.
REQUIRED_KEYS = (
    "clinic", "contact", "hours", "nav", "actions", "hero", "booking", "stats",
    "services", "why_us", "doctors", "staff", "assistant", "journey",
    "facility", "testimonials", "faq", "footer",
)

# where the patient portal lives in THIS deployment. the yaml carries a local
# default; the environment wins, so a tunnel hostname never has to be written
# into a file that is committed.
PORTAL_ENV = "PATIENT_PORTAL_URL"


def load(path=None, env=None):
    path = Path(path) if path else CONFIG_PATH
    env = os.environ if env is None else env
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} did not parse to a mapping")
    missing = [k for k in REQUIRED_KEYS if k not in data]
    if missing:
        raise RuntimeError(f"{path} is missing required section(s): {', '.join(missing)}")

    # both sign-in links are derived, so they cannot disagree with each other
    # or drift back to the staff app
    actions = data["actions"]
    portal = (env.get(PORTAL_ENV) or actions["portal_url"]).rstrip("/")
    actions["portal_url"] = portal
    actions["login_href"] = portal + "/login"
    actions["assistant_signin_href"] = portal + "/login"
    return data


# a loopback portal is the right default for a demo and the wrong one for a
# deployment: this is the public site, so every visitor who clicks Login would
# be sent to their OWN machine. it fails quietly - the link is there, it just
# goes nowhere - and the yaml default is committed, so shipping it is the
# path of least resistance rather than an unlikely mistake.
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0")


def portal_guard_refusal(env, portal_url):
    """Why the public site must not start, or None. Pure, so every branch is testable.

    Takes the RESOLVED portal url, not the environment variable: an operator who
    edits portal_url in clinic.yaml has configured the deployment just as well
    as one who exports PATIENT_PORTAL_URL, and refusing that would be refusing a
    correct setup.
    """
    if not codice_fiscale.is_production(env):
        return None
    if not portal_url:
        return f"{PORTAL_ENV} is not set and clinic.yaml carries no portal_url"
    host = (urlsplit(portal_url).hostname or "").lower()
    if host in LOCAL_HOSTS or host.endswith(".localhost"):
        return (f"the patient portal points at {host} while CLINIC_ENV=production - "
                f"set {PORTAL_ENV} to the address patients actually reach")
    return None


def portal_guard_or_exit(env=None):
    # startup guard for site_run.py, same shape as disk_guard and
    # codice_fiscale.guard_or_exit
    import sys
    env = os.environ if env is None else env
    reason = portal_guard_refusal(env, load(env=env)["actions"]["portal_url"])
    if reason:
        print(f"refusing to start: {reason}", file=sys.stderr)
        sys.exit(1)
