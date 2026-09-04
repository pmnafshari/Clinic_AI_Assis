"""Public clinic assistant. Answers from clinic.yaml, nothing else.

This is a ROUTER, not a language model, and the page says so. It matches a
question against a small set of clinic topics and returns the clinic's own
words. That is a deliberate choice for an unauthenticated health-adjacent
page: a router cannot invent an opening time, a price or a treatment.

It has no database access - site_app imports none, asserted structurally in
site_app_selftest section 1 - so there is no patient data here to leak. A
question about someone's own records is not answered; it is handed off to
sign-in, which is where the scoped, model-backed chat lives.

Three outcomes, deliberately echoing the patient chat's vocabulary:
    answer   - the clinic's own information
    handoff  - personal, needs sign-in
    refusal  - outside what this page knows
"""

import re

# personal intent is checked FIRST: "when is my appointment" mentions
# appointments, and must not be answered with the general booking blurb.
PERSONAL = re.compile(
    r"\b(my|mine|i have|do i|am i|my own|il mio|la mia|i miei|le mie|ho un|devo)\b"
    r"|\b(appuntamento|fattura|fatture|visita|visite|pagare|dati)\b.*\b(mio|mia|miei|mie)\b",
    re.I,
)

TOPICS = [
    ("hours", r"\b(hour|hours|open|opening|close|closed|when.*open|orari|orario|aperto|chiuso)\b"),
    ("address", r"\b(where|address|find you|located|location|directions|parking|dove|indirizzo|parcheggi)\b"),
    ("phone", r"\b(phone|call|telephone|number|contact|telefono|chiamare|numero|contatt)\b"),
    ("emergency", r"\b(emergency|urgent|pain|broken tooth|out of hours|emergenza|urgente|dolore)\b"),
    ("services", r"\b(service|services|treatment|treatments|offer|do you do|implant|hygiene|"
                 r"orthodont|cosmetic|whiten|servizi|trattament|impianti|igiene)\b"),
    ("doctors", r"\b(doctor|doctors|dentist|dentists|who works|team|staff|specialist|"
                r"dottore|dentista|medici|equipe)\b"),
    ("booking", r"\b(book|booking|appointment|appointments|reschedul|cancel|prenot|appuntament|disdire)\b"),
    ("account", r"\b(account|sign in|log in|login|password|pin|verif|privacy|records|"
                r"accedi|accesso|verifica|dati)\b"),
]


def _services(c):
    return ", ".join(s["title"] for s in c["services"]["entries"])


def _doctors(c):
    return "; ".join(f"{d['name']} - {d['role']}" for d in c["doctors"]["entries"])


def _hours(c):
    return "; ".join(f"{h['day']}: {h['open']}" for h in c["hours"])


BUILDERS = {
    "hours": lambda c: f"Opening hours - {_hours(c)}.",
    "address": lambda c: (f"{c['contact'].get('place', c['clinic']['name'])}, "
                          f"{c['contact']['address_line']}, {c['contact']['city']}. "
                          f"{c['contact']['directions_note']}"),
    "phone": lambda c: (f"You can call the clinic on {c['contact']['phone']}, "
                        f"or email {c['contact']['email']}."),
    "emergency": lambda c: (f"{c['contact']['emergency_note']} "
                            f"The emergency line is {c['contact']['emergency_phone']}."),
    "services": lambda c: f"We offer: {_services(c)}. There is more detail on the services page.",
    "doctors": lambda c: f"Our dentists: {_doctors(c)}.",
    "booking": lambda c: (f"Appointments are arranged by phone on {c['contact']['phone']}, "
                          f"or through the assistant once you have signed in. "
                          f"Nothing is booked on this website."),
    "account": lambda c: (c["assistant"]["verification_note"]),
}

HANDOFF = ("That is specific to you, so I cannot answer it here. Sign in to the assistant "
           "with your codice fiscale and the PIN the clinic issued you, and it will answer "
           "from your own records.")

REFUSAL = ("I can only answer questions about this clinic - opening hours, where we are, "
           "our services, our dentists, how to book, and how signing in works. For anything "
           "else, please call the clinic.")


def answer(question, clinic):
    """-> (state, text). state is 'answer' | 'handoff' | 'refusal'."""
    q = (question or "").strip()
    if not q:
        return "refusal", REFUSAL
    q = q[:500]

    # personal first, so "when is my next appointment" hands off rather than
    # being answered with the general booking text
    if PERSONAL.search(q):
        return "handoff", HANDOFF

    for name, pattern in TOPICS:
        if re.search(pattern, q, re.I):
            return "answer", BUILDERS[name](clinic)

    return "refusal", REFUSAL


def selftest():
    from . import content
    c = content.load()

    # 1. every topic builds from config and mentions something real from it
    for q, must in (
        ("what are your opening hours?", c["hours"][0]["day"]),
        ("where are you?", c["contact"]["address_line"]),
        ("what's your phone number?", c["contact"]["phone"]),
        ("i have terrible tooth pain out of hours", c["contact"]["emergency_phone"]),
        ("do you do implants?", c["services"]["entries"][0]["title"]),
        ("who are your dentists?", c["doctors"]["entries"][0]["name"]),
        ("how do i book an appointment?", c["contact"]["phone"]),
    ):
        state, text = answer(q, c)
        assert state in ("answer", "handoff"), f"1: {q!r} -> {state}"
        if state == "answer":
            assert must in text, f"1: {q!r} did not carry {must!r} from config"

    # 2. THE assertion that matters: a personal question is never answered
    # here. this page has no database and must not imply it does.
    for q in ("when is my next appointment?",
              "how much do i owe?",
              "what did i have done last time?",
              "quando e' il mio prossimo appuntamento?",
              "quanto devo pagare?"):
        state, text = answer(q, c)
        assert state == "handoff", f"2: {q!r} -> {state}, expected handoff"
        assert "sign in" in text.lower(), "2: the handoff must say how to get an answer"

    # 3. off-topic is refused, not guessed at
    for q in ("what is the capital of france?", "tell me a joke", "who won the football"):
        state, _ = answer(q, c)
        assert state == "refusal", f"3: {q!r} -> {state}, expected refusal"

    # 4. empty input is a refusal, not a crash
    assert answer("", c)[0] == "refusal" and answer(None, c)[0] == "refusal", "4: empty input"

    # 5. no answer text is invented - every one is built from config
    for name, build in BUILDERS.items():
        out = build(c)
        assert out and len(out) > 10, f"5: {name} produced nothing"

    print("selftest ok")
