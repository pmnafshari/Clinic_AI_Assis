from dental_notes_schema import CF_PATTERN
from storage import init_db, lookup_patient

FIELD_CUES = {
    "phone": "phone",
    "number": "phone",
    "invoice": "invoice",
    "amount": "invoice",
    "appointment": "appointment",
    "date": "date",
    "when": "date",
}


def classify_question(question):
    q = question.lower()
    for cue in FIELD_CUES:
        if cue in q:
            return "exact"
    return "meaning"


def field_for_question(question):
    q = question.lower()
    for cue, field in FIELD_CUES.items():
        if cue in q:
            return field
    return None


def resolve_cf(name, conn):
    rows = conn.execute(
        "SELECT codice_fiscale FROM patients WHERE lower(patient_name) = lower(?)",
        (name,),
    ).fetchall()
    if len(rows) == 0:
        return None
    if len(rows) > 1:
        return [r["codice_fiscale"] for r in rows if CF_PATTERN.match(r["codice_fiscale"])]
    cf = rows[0]["codice_fiscale"]
    if not CF_PATTERN.match(cf):
        return None
    return cf
