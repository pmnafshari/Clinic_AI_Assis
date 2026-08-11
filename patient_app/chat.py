"""the request path for a patient's question.

this module runs the no-advice gate before anything else, routes deterministically
with no model anywhere in the routing decision, and calls a model only to phrase
rows that have already been fetched and scoped. §3.5 of the architecture doc puts
the intent gate at step 3 and the accessor call at step 4 - that ordering is a
binding security property (§7.1), not a style choice, and it is enforced here by
plain code structure: each step in answer_question is a separate early return, so
the gate always runs before the accessor and the accessor always runs before the
model.
"""

import re
import unicodedata

ROUTES = ("next_appointment", "invoices", "demographics", "visits")

# --- the D-02 pre-retrieval gate ---
#
# [ASSUMED] this italian vocabulary has had no native-speaker review. §7.1
# marks gate vocabulary and thresholds as explicitly non-binding, and phase 19
# tunes it against the patient_deflect rows this phase starts writing.
#
# checked in the order below, not the order a reader might expect from §4.2's
# table: "what should i take" (treatment) and "should i" (advice) overlap as
# substrings, and checking advice first would deflect "What should I take for
# this?" as advice instead of routing it to the treatment category the UI-SPEC
# acceptance case expects. treatment and symptom vocabulary do not collide
# with each other, so this reorder only resolves the one real conflict.
_ADVICE_CATEGORY_ORDER = ("treatment", "symptom", "advice")

ADVICE_TRIGGERS = {
    # do not add a bare "devo" here - UI-SPEC example chip 3 is "Quanto devo
    # pagare?", a pure invoice question, and a bare "devo" would deflect it.
    # only the multi-word advice phrasings below are safe.
    "advice": (
        r"dovrei", r"cosa devo fare", r"devo fare", r"e normale", r"e' normale",
        r"va bene se", r"should i", r"is this normal", r"do i need",
    ),
    "symptom": (
        r"dolore", r"fa male", r"mi fa male", r"gonfiore", r"gonfio", r"sanguina",
        r"sangue", r"infiammazione", r"ascesso", r"febbre",
        r"pain", r"hurts?", r"swelling", r"swollen", r"bleeding", r"abscess", r"fever",
    ),
    # do not add a bare "cura" here - "cura canalare" is a rendered procedure
    # name, and "quando ho fatto la cura canalare?" is a legitimate records
    # question, not a treatment request.
    "treatment": (
        r"antibiotico", r"antidolorifico", r"medicina", r"farmaco", r"cosa posso prendere",
        r"antibiotic", r"painkiller", r"medicine", r"medication", r"what should i take",
    ),
}


def normalise(text):
    """lowercase, NFKD, combining marks stripped, whitespace collapsed."""
    text = text.lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text).strip()


def advice_category(question):
    """-> 'advice' | 'symptom' | 'treatment' | None. Runs on raw text, normalises
    internally. Pure - reads only the question text, so nothing can be fetched
    from inside it.
    """
    text = normalise(question)
    for category in _ADVICE_CATEGORY_ORDER:
        for pattern in ADVICE_TRIGGERS[category]:
            if re.search(rf"\b{pattern}\b", text):
                return category
    return None


# --- the D-01 deterministic classifier ---
#
# same dict-of-cues shape as ask.FIELD_CUES/field_for_question, extended to
# four destinations and two languages. evaluated in ROUTES order, most
# specific to broadest, so a question matching two tables resolves the same
# way every time.
ROUTE_TRIGGERS = {
    "next_appointment": (
        r"prossimo appuntamento", r"appuntamento", r"prossima visita", r"quando torno",
        r"next appointment", r"appointment", r"when.*come back",
    ),
    "invoices": (
        r"fattura", r"fatture", r"pagare", r"pagamento", r"quanto devo", r"costo", r"importo",
        r"conto", r"invoice", r"bill", r"owe", r"cost", r"amount", r"pay",
    ),
    # anagrafic\w* covers anagrafici/anagrafica/anagrafico - the stem alone
    # would either miss the real word or (with a bare trailing \b) never
    # match it at all, since none of those inflections is the literal
    # string "anagrafic".
    "demographics": (
        r"telefono", r"numero", r"recapito", r"miei dati", r"anagrafic\w*", r"come mi chiamo",
        r"phone", r"number", r"contact", r"my details", r"my name",
    ),
    # intervent\w*/trattament\w* cover intervento/interventi and
    # trattamento/trattamenti the same way anagrafic\w* does above.
    "visits": (
        r"visita", r"visite", r"che visite", r"cosa ho fatto", r"storico", r"intervent\w*",
        r"trattament\w*", r"visit", r"visits", r"procedure", r"treatment", r"what have i had",
    ),
}


def route_question(question):
    """-> one of ROUTES, or None when nothing matches. Pure, deterministic, no
    model - reads only the question text and never reaches the database.
    """
    text = normalise(question)
    for route in ROUTES:
        for pattern in ROUTE_TRIGGERS[route]:
            if re.search(rf"\b{pattern}\b", text):
                return route
    return None
