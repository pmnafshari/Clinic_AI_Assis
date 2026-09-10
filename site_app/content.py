"""Clinic content, read once from clinic.yaml.

UX-07: no name, figure, price or opening hour is written into a template. The
clinic owner changes this file, not Jinja. pyyaml is already a dependency of
the project, so this adds nothing to install.
"""

import os
from pathlib import Path

import yaml

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
