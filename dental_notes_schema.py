from datetime import date
from typing import Optional

from pydantic import BaseModel


class Invoice(BaseModel):
    description: str
    amount: float


class DentalNote(BaseModel):
    patient_name: str
    codice_fiscale: str
    phone: Optional[str] = None
    visit_date: Optional[date] = None
    procedures: list[str] = []
    invoices: list[Invoice] = []
    notes_text: str = ""
