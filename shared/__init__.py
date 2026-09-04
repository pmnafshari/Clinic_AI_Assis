"""Assets shared by all three apps - tokens, components, fonts.

One directory on disk. Each app serves it through its own /shared route
rather than keeping a copy, which is the same shape patient_app already
uses for the vendored bundle.
"""
from pathlib import Path

STATIC_ROOT = Path(__file__).resolve().parent / "static"
