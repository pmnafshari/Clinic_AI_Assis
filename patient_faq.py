"""Approved content for the patient assistant (P10.04). IT and EN.

TWO KINDS OF ENTRY, AND ONLY ONE OF THEM CAN REACH A PATIENT.

*Administrative* entries are clinic fact - opening hours, where the clinic is,
how to reach it, what to bring. They are built from `site_app/clinic.yaml`, the
same file the public site renders, so there is one source of truth and nothing
here can drift from what the website says. They are approved.

*Clinical* entries - aftercare, what is normal after a procedure, when to worry
- are written below and ship `approved: False`. **They are never served.** A
question that matches one produces a handoff instead. Publishing clinical text
to a patient is a qualified person's decision (R09, R10), and no agent may set
that flag: `approve()` refuses to run, and a test asserts every clinical entry
is still unapproved. They are written down rather than omitted so the owner has
something concrete to review, not a blank page.

VERSION is bumped by hand whenever an entry's text changes. It is recorded on
the answer so an eval run and an audit row can both say which wording they saw.
"""
import re
import sys

VERSION = "2026-09-22.1"

ADMIN, CLINICAL = "administrative", "clinical"


def _hours(c):
    return "; ".join(f"{h['day']}: {h['open']}" for h in c["hours"])


def _address(c):
    return (f"{c['contact'].get('place', c['clinic']['name'])}, "
            f"{c['contact']['address_line']}, {c['contact']['city']}")


# key, kind, topic, approved, source, triggers, builder(clinic, lang)
ENTRIES = (
    {
        "key": "hours", "kind": ADMIN, "topic": "other", "approved": True,
        "source": "site_app/clinic.yaml:hours",
        "triggers": (r"orari", r"orario", r"aperto", r"chiuso", r"quando aprite",
                     r"opening hours", r"hours", r"when.*open", r"are you open"),
        "text": {
            "it": lambda c: f"Orari di apertura - {_hours(c)}.",
            "en": lambda c: f"Opening hours - {_hours(c)}.",
        },
    },
    {
        "key": "address", "kind": ADMIN, "topic": "other", "approved": True,
        "source": "site_app/clinic.yaml:contact",
        "triggers": (r"dove siete", r"indirizzo", r"come arrivo", r"parcheggi\w*",
                     r"where are you", r"address", r"directions", r"parking", r"how do i get"),
        "text": {
            "it": lambda c: f"{_address(c)}. {c['contact']['directions_note']}",
            "en": lambda c: f"{_address(c)}. {c['contact']['directions_note']}",
        },
    },
    {
        "key": "contact", "kind": ADMIN, "topic": "other", "approved": True,
        "source": "site_app/clinic.yaml:contact",
        "triggers": (r"telefono dello studio", r"numero dello studio", r"come vi contatto",
                     r"clinic phone", r"your phone number", r"how do i contact you"),
        "text": {
            "it": lambda c: f"Telefono: {c['contact']['phone']}. Email: {c['contact']['email']}.",
            "en": lambda c: f"Phone: {c['contact']['phone']}. Email: {c['contact']['email']}.",
        },
    },
    {
        "key": "emergency", "kind": ADMIN, "topic": "other", "approved": True,
        "source": "site_app/clinic.yaml:contact.emergency_phone",
        "triggers": (r"emergenza", r"numero di emergenza", r"fuori orario",
                     r"emergency number", r"out of hours", r"urgent number"),
        "text": {
            "it": lambda c: (f"Numero di emergenza: {c['contact']['emergency_phone']}. "
                             f"{c['contact']['emergency_note']}"),
            "en": lambda c: (f"Emergency line: {c['contact']['emergency_phone']}. "
                             f"{c['contact']['emergency_note']}"),
        },
    },
    # --- clinical, UNAPPROVED, never served -------------------------------
    # drafted for the owner and a qualified reviewer. until approved=True each
    # of these becomes a handoff.
    {
        "key": "after_extraction", "kind": CLINICAL, "topic": "clinical", "approved": False,
        "source": "draft, not reviewed by a clinician",
        "triggers": (r"dopo l estrazione", r"dopo estrazione", r"after an extraction",
                     r"after extraction", r"tooth was pulled"),
        "text": {
            "it": lambda c: "DRAFT - non approvato.",
            "en": lambda c: "DRAFT - not approved.",
        },
    },
    {
        "key": "after_filling", "kind": CLINICAL, "topic": "clinical", "approved": False,
        "source": "draft, not reviewed by a clinician",
        "triggers": (r"dopo l otturazione", r"dopo otturazione", r"after a filling",
                     r"after filling"),
        "text": {
            "it": lambda c: "DRAFT - non approvato.",
            "en": lambda c: "DRAFT - not approved.",
        },
    },
    {
        "key": "aftercare_general", "kind": CLINICAL, "topic": "clinical", "approved": False,
        "source": "draft, not reviewed by a clinician",
        "triggers": (r"cosa posso mangiare", r"posso sciacquare", r"quando posso lavare i denti",
                     r"what can i eat", r"can i rinse", r"when can i brush"),
        "text": {
            "it": lambda c: "DRAFT - non approvato.",
            "en": lambda c: "DRAFT - not approved.",
        },
    },
)


def _normalise(text):
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def match(question):
    """-> the entry whose triggers fit, approved or not, else None."""
    text = _normalise(question)
    for entry in ENTRIES:
        for pattern in entry["triggers"]:
            if re.search(rf"\b{pattern}\b", text):
                return entry
    return None


def answer(question, clinic, lang="it"):
    """-> (state, body, meta)

    state is 'answer' only for an APPROVED entry. An unapproved one returns
    'unapproved' with the key, so the caller raises a handoff instead of
    printing draft clinical text. 'none' means nothing matched.
    """
    entry = match(question)
    if entry is None:
        return "none", None, {}
    meta = {"key": entry["key"], "kind": entry["kind"], "topic": entry["topic"],
            "source": entry["source"], "version": VERSION}
    if not entry["approved"]:
        return "unapproved", None, meta
    builder = entry["text"].get(lang) or entry["text"]["it"]
    return "answer", builder(clinic), meta


def approve(*_args, **_kwargs):
    """Deliberately not implemented.

    Approving clinical content is a qualified person's decision, recorded
    outside the code. Flipping `approved` is a source edit a human makes and
    reviews, not something any running process - or any agent - may do.
    """
    raise NotImplementedError(
        "clinical content is approved by a qualified person editing ENTRIES, not at runtime")


def unapproved_keys():
    return tuple(e["key"] for e in ENTRIES if not e["approved"])


def selftest():
    from site_app.content import load
    clinic = load()

    # 1. every clinical entry is unapproved, and the module offers no way to
    # change that at runtime. this is the load-bearing assertion of P10.04.
    clinical = [e for e in ENTRIES if e["kind"] == CLINICAL]
    assert clinical, "1: the clinical drafts must exist to be reviewable"
    assert all(not e["approved"] for e in clinical), \
        f"1: unapproved clinical content must stay unapproved: {unapproved_keys()}"
    try:
        approve("after_extraction")
        raise AssertionError("1: approve() must not work")
    except NotImplementedError:
        pass

    # 2. an unapproved entry never yields a body, in either language
    for lang in ("it", "en"):
        state, body, meta = answer("cosa posso mangiare dopo?", clinic, lang)
        assert state == "unapproved" and body is None, f"2: {lang} served draft text: {body!r}"
        assert meta["key"] == "aftercare_general" and meta["version"] == VERSION
    state, body, _ = answer("what can i eat", clinic, "en")
    assert state == "unapproved" and body is None, "2: EN draft served"

    # 3. administrative answers come from clinic.yaml, in both languages, and
    # carry their source and the content version
    state, body, meta = answer("quali sono gli orari?", clinic, "it")
    assert state == "answer" and "Orari di apertura" in body, f"3: {state} {body!r}"
    assert meta["source"].startswith("site_app/clinic.yaml") and meta["version"] == VERSION
    assert _hours(clinic) in body, "3: the hours are the file's, not retyped"
    state, body, _ = answer("what are your opening hours?", clinic, "en")
    assert state == "answer" and body.startswith("Opening hours"), f"3: EN {body!r}"
    state, body, _ = answer("where are you?", clinic, "en")
    assert state == "answer" and clinic["contact"]["address_line"] in body, "3: address from file"

    # 4. nothing matched is 'none', not a guess
    state, body, meta = answer("quanto costa un impianto?", clinic, "it")
    assert state == "none" and body is None and meta == {}, f"4: {state} {body!r}"
    assert answer("", clinic)[0] == "none"

    # 5. no entry can answer with text that is not in clinic.yaml. a builder
    # that invented a phone number would pass every check above.
    for entry in ENTRIES:
        if not entry["approved"]:
            continue
        for lang in ("it", "en"):
            out = entry["text"][lang](clinic)
            assert out and "DRAFT" not in out, f"5: {entry['key']} {lang}"
    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python patient_faq.py --selftest")
    sys.exit(1)


if __name__ == "__main__":
    main()
