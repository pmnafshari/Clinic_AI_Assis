import json
import sys
import urllib.error
import urllib.request

from dental_notes_schema import KNOWN_PROCEDURES, DentalNote

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "dental-notes"


class OllamaUnreachable(Exception):
    pass


def extract_json(text):
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def parse_reply(reply, fallback_cf=None):
    # reply -> validated DentalNote. Raises ValueError when the output is not
    # schema-valid so a half-formed or wrong-shape record never passes silently.
    #
    # fallback_cf is for callers that already know the patient (the ?cf= note
    # form). Most notes don't spell out a codice fiscale, so the model returns
    # "" and validation would reject the whole extraction over a field the
    # caller owns and is about to overwrite anyway. It only fills a blank -
    # a cf the model actually read is left alone so the mismatch notice can
    # still catch a note pasted under the wrong patient.
    obj = extract_json(reply)
    if obj is None:
        raise ValueError("model did not return valid JSON")
    if fallback_cf and not obj.get("codice_fiscale"):
        obj["codice_fiscale"] = fallback_cf
    try:
        note = DentalNote(**obj)
    except Exception as e:
        raise ValueError("model output failed schema validation: " + str(e))
    unknown = note.unknown_procedures()
    if unknown:
        print("unknown procedure code, needs review:", ", ".join(unknown))
    return note


def call_model(note, urlopen=urllib.request.urlopen):
    payload = {
        "model": MODEL,
        "prompt": note,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(OLLAMA_URL, data=data, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=120) as resp:
            body = json.load(resp)
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        # refused connection, a 200 with a non-JSON body, or a read timeout all
        # mean Ollama isn't usable right now - never let it 500 a route
        raise OllamaUnreachable("Ollama not reachable - run: ollama run dental-notes")
    return body.get("response", "")


def extract_note(note):
    return parse_reply(call_model(note))


def selftest():
    # 1. valid output validates
    good = ('{"patient_name": "mario rossi", "codice_fiscale": "MRRS800010150100", '
            '"phone": null, "visit_date": null, "procedures": ["rct 26"], '
            '"invoices": [], "clinical_notes": "rct done", "next_appointment": "14d"}')
    note = parse_reply(good)
    assert note.patient_name == "mario rossi"
    assert note.phone is None
    assert note.clinical_notes == "rct done"
    assert note.next_appointment == "14d"

    # 2. malformed (non-JSON) output is rejected
    try:
        parse_reply("sorry, I cannot do that")
        raise AssertionError("malformed output should have been rejected")
    except ValueError:
        pass

    # 3. missing required field (no codice_fiscale) is flagged
    try:
        parse_reply('{"patient_name": "anna bianchi"}')
        raise AssertionError("missing required field should have been flagged")
    except ValueError:
        pass

    # 3b. a codice_fiscale that fails the v2 regex (^[A-Z]{4}[0-9]{12}$) is flagged,
    # not silently accepted - structural guard for a malformed key field.
    try:
        parse_reply('{"patient_name": "anna bianchi", "codice_fiscale": "not-a-cf"}')
        raise AssertionError("invalid codice_fiscale should have been flagged")
    except ValueError:
        pass

    # 3c. a caller that already knows the patient can supply the codice fiscale.
    # real dentist shorthand rarely spells one out, so the model returns "" and
    # the whole extraction used to be rejected over a field the caller owns.
    no_cf = '{"patient_name": "anna bianchi", "codice_fiscale": "", "procedures": []}'
    note = parse_reply(no_cf, fallback_cf="BNCA850010150300")
    assert note.codice_fiscale == "BNCA850010150300", "3c: fallback_cf should fill an empty cf"

    # 3d. the fallback fills a gap, it never overrides what the model did read -
    # otherwise a note pasted under the wrong patient would be silently relabelled
    # instead of raising the mismatch notice.
    other_cf = ('{"patient_name": "giulia neri", "codice_fiscale": "NREG900010150400", '
                '"procedures": []}')
    note = parse_reply(other_cf, fallback_cf="BNCA850010150300")
    assert note.codice_fiscale == "NREG900010150400", "3d: fallback_cf must not override a real cf"

    # 3e. the fallback is not a way past the regex - a malformed non-empty cf is
    # still rejected, and an empty one still fails when no caller supplies a cf
    try:
        parse_reply('{"patient_name": "anna bianchi", "codice_fiscale": "not-a-cf"}',
                    fallback_cf="BNCA850010150300")
        raise AssertionError("3e: a malformed codice_fiscale should still be flagged")
    except ValueError:
        pass
    try:
        parse_reply(no_cf)
        raise AssertionError("3e: an empty codice_fiscale should still fail without a fallback")
    except ValueError:
        pass

    # 4. semantic hallucination: the note has no phone but the output invents one.
    # Pydantic CANNOT catch this - the value has the right type, so it validates.
    # This is intentional and out of scope per 02-CONTEXT.md: the defenses are the
    # prompt-level guard (Modelfile SYSTEM) plus the eval gate, NOT per-call
    # rejection. Asserting it validates documents the limitation so no one assumes
    # this self-test catches semantic hallucination.
    #
    # section 6 narrows this for one field only: an invented *procedure code* is
    # now flagged (not rejected). Everything else here still stands - an invented
    # phone, name or date passes exactly as before.
    hallucinated = ('{"patient_name": "luca verdi", "codice_fiscale": "VRDL900010150100", '
                    '"phone": "333 0000000", "visit_date": null, "procedures": [], '
                    '"invoices": [], "clinical_notes": "", "next_appointment": null}')
    note = parse_reply(hallucinated)
    assert note.phone == "333 0000000"  # passes validation despite being invented

    # 6. an unknown procedure code is flagged for review, never rejected. a note
    # with a code nobody recognises is more likely a model invention than a new
    # treatment, but refusing it would lose a real clinical record over a
    # vocabulary gap - so it validates and the caller gets something to show a
    # human. written first, observed failing.
    invented = ('{"patient_name": "luca verdi", "codice_fiscale": "VRDL900010150100", '
                '"procedures": ["banana 38", "filling 47"], "invoices": [], '
                '"clinical_notes": "", "next_appointment": null}')
    note = parse_reply(invented)
    assert note.procedures == ["banana 38", "filling 47"], "6: the note must still validate"
    assert note.unknown_procedures() == ["banana"], \
        f"6: expected banana flagged, got {note.unknown_procedures()}"

    # 6b. every code in the glossary is recognised, tooth number or not
    known = ('{"patient_name": "anna bianchi", "codice_fiscale": "BNCA850010150300", '
             '"procedures": ["rct 26", "x-ray 16", "prophy", "seal 16"], "invoices": [], '
             '"clinical_notes": "", "next_appointment": null}')
    assert parse_reply(known).unknown_procedures() == [], \
        "6b: glossary codes must not be flagged"

    # 6c. sigillatura is a sealant, not a filling - it has its own code so a
    # preventive procedure never lands on the record as a restorative one
    assert "seal" in KNOWN_PROCEDURES, "6c: seal should be in the glossary"
    assert KNOWN_PROCEDURES["seal"] != KNOWN_PROCEDURES.get("filling"), \
        "6c: seal and filling must stay distinct"

    # 6d. case and stray whitespace do not smuggle a code past the check
    messy = ('{"patient_name": "anna bianchi", "codice_fiscale": "BNCA850010150300", '
             '"procedures": ["  RCT 26", "Banana"], "invoices": [], '
             '"clinical_notes": "", "next_appointment": null}')
    assert parse_reply(messy).unknown_procedures() == ["banana"], \
        "6d: matching should be case- and whitespace-insensitive"

    # 5. unreachable Ollama gives a clear, distinct error
    def boom(*a, **k):
        raise urllib.error.URLError("connection refused")

    try:
        call_model("any note", urlopen=boom)
        raise AssertionError("unreachable Ollama should raise OllamaUnreachable")
    except OllamaUnreachable as e:
        assert "ollama run dental-notes" in str(e)

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    if len(sys.argv) < 2:
        print('usage: python extract_note.py "<note>"  |  python extract_note.py --selftest')
        sys.exit(1)
    try:
        result = extract_note(sys.argv[1])
    except OllamaUnreachable as e:
        print(e)
        sys.exit(1)
    except ValueError as e:
        print("rejected:", e)
        sys.exit(1)
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
