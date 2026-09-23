"""Whether a shorthand code has clinical approval (P13.01, POL-11).

The glossary (dental_shorthand_glossary.json) says what a code means. This file
says whether anyone qualified has agreed. Nothing that turns a code into
clinical meaning for a reader - the patient portal, the chat, a summary's
contradiction check - may use a code this says is not approved.

APPROVED MEANS ALL OF: the register names a clinical owner; the entry is marked
approved with who and when; and the register was made against the glossary as
it is now (sha256). A missing, unreadable or out-of-date register approves
nothing. No clinical owner has been named, so today nothing is approved.
"""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REGISTER_PATH = ROOT / "glossary_review.json"
GLOSSARY_PATH = ROOT / "dental_shorthand_glossary.json"


def register():
    """The register if it is current and names an owner, else None."""
    try:
        reg = json.loads(Path(REGISTER_PATH).read_text())
        glossary_sha = hashlib.sha256(Path(GLOSSARY_PATH).read_bytes()).hexdigest()
    except (OSError, ValueError):
        return None
    if not isinstance(reg, dict) or reg.get("glossary_sha256") != glossary_sha:
        return None
    if not str(reg.get("clinical_owner") or "").strip():
        return None
    return reg


def approved(code):
    reg = register()
    if reg is None:
        return False
    entry = (reg.get("entries") or {}).get(str(code or "").strip().lower())
    if not isinstance(entry, dict):
        return False
    return (entry.get("status") == "approved" and bool(entry.get("approved_by"))
            and bool(entry.get("approved_at")))


def pending_count():
    """How many glossary codes may not be interpreted yet."""
    codes = json.loads(Path(GLOSSARY_PATH).read_text())
    return sum(1 for code in codes if not approved(code)), len(codes)
