"""P29 U-009: the short lists behind the reason boxes. a form sends reason_choice (one of the
list, or other) and reason (what was typed). other, nothing chosen or a value that is not on the
list counts only the typed words, so choosing never stands in for a reason that has to be given."""

DECLINE = ("No free time in that period", "Dentist not available", "Patient asked to withdraw it",
           "Already booked")
REJECT_NOTE = ("Wrong patient", "Not a dental note", "Already filed", "Cannot be read")
STOCK = ("Weekly count", "Damaged", "Expired", "Entered by mistake")


def chosen(form, choices):
    choice = (form.get("reason_choice") or "").strip()
    typed = (form.get("reason") or "").strip()
    if choice not in choices:
        return typed
    return f"{choice}: {typed}" if typed else choice
