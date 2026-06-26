import re
from datetime import date
from typing import Optional

from pydantic import BaseModel, field_validator

CF_PATTERN = re.compile(r'^[A-Z]{4}[0-9]{12}$')


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
        if not CF_PATTERN.match(v):
            raise ValueError(f'codice_fiscale must match ^[A-Z]{{4}}[0-9]{{12}}$, got {v!r}')
        return v
