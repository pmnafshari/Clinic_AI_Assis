import json
from datetime import date
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, field_validator

from codice_fiscale import is_valid as is_valid_cf


# anchored on this file, not the process cwd - the app, the watcher and the
# selftests all start from different directories
GLOSSARY_PATH = Path(__file__).resolve().parent / "dental_shorthand_glossary.json"
KNOWN_PROCEDURES = json.loads(GLOSSARY_PATH.read_text())


class Invoice(BaseModel):
    amount: float
    description: str


class DentalNote(BaseModel):
    patient_name: str
    codice_fiscale: str
    phone: Optional[str] = None
    visit_date: Optional[date] = None
    procedures: list[str] = []
    invoices: list[Invoice] = []
    clinical_notes: str = ""
    next_appointment: Optional[str] = None

    @field_validator('codice_fiscale')
    @classmethod
    def validate_cf(cls, v):
        if not is_valid_cf(v):
            raise ValueError(f'not a valid codice fiscale: {v!r}')
        return v

    def unknown_procedures(self):
        # codes the glossary does not recognise. deliberately NOT a validator: a
        # code nobody knows is usually the model inventing one, but it can also
        # be a real treatment the glossary has not caught up with, and refusing
        # the note would lose a clinical record over a vocabulary gap. flag it,
        # let a human decide.
        unknown = []
        for entry in self.procedures:
            code = entry.strip().split(" ")[0].lower()
            if code and code not in KNOWN_PROCEDURES:
                unknown.append(code)
        return unknown
