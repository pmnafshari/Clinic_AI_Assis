"""Clinic content, read once from clinic.yaml.

UX-07: no name, figure, price or opening hour is written into a template. The
clinic owner changes this file, not Jinja. pyyaml is already a dependency of
the project, so this adds nothing to install.
"""

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


def load(path=None):
    path = Path(path) if path else CONFIG_PATH
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} did not parse to a mapping")
    missing = [k for k in REQUIRED_KEYS if k not in data]
    if missing:
        raise RuntimeError(f"{path} is missing required section(s): {', '.join(missing)}")
    return data
