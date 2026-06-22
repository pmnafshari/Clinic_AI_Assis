import json
import string
import sys

from pydantic import ValidationError

from dental_notes_schema import DentalNote

GLOSSARY_FILE = "dental_shorthand_glossary.json"
SAMPLES_FILE = "sample_notes.txt"

# shorthand that is clearly not normal english - if a sample uses one of these
# it must be explained in the glossary
KNOWN_SHORTHAND = ["rct", "ext", "comp", "perio", "opg", "abx", "fu", "prophy"]


def check_glossary_loads():
    with open(GLOSSARY_FILE) as f:
        glossary = json.load(f)
    print("ok - glossary loaded,", len(glossary), "entries")
    return glossary


def check_schema_instantiates():
    note = DentalNote(
        patient_name="mario rossi",
        codice_fiscale="RSSMRA80A01H501U",
        phone="333 1234567",
        visit_date="2026-06-22",
        procedures=["rct 26"],
        invoices=[{"description": "scaling", "amount": 50.0}],
        notes_text="mild caries on 27, follow up in 2 weeks",
    )
    print("ok - valid note instantiated for", note.patient_name)


def check_schema_rejects_bad_data():
    bad = {"patient_name": "anna", "invoices": [{"description": "x", "amount": "free"}]}
    try:
        DentalNote(**bad)
    except ValidationError:
        print("ok - invalid note rejected")
        return
    print("FAIL - invalid note was accepted")
    sys.exit(1)


def check_token_coverage(glossary):
    words = set()
    with open(SAMPLES_FILE) as f:
        for line in f:
            for word in line.split():
                words.add(word.strip(string.punctuation).lower())

    used = [t for t in KNOWN_SHORTHAND if t in words]
    missing = [t for t in used if t not in glossary]

    if missing:
        print("FAIL - shorthand used in notes but missing from glossary:", missing)
        sys.exit(1)
    if len(used) < 3:
        print("FAIL - sample notes barely use any shorthand:", used)
        sys.exit(1)
    print("ok - all shorthand in samples covered,", len(used), "tokens used")


def main():
    glossary = check_glossary_loads()
    check_schema_instantiates()
    check_schema_rejects_bad_data()
    check_token_coverage(glossary)
    print("all checks passed")


if __name__ == "__main__":
    main()
