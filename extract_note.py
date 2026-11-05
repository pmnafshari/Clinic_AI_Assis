import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from codice_fiscale import is_valid as is_valid_cf, name_letters, normalize as normalize_cf
from dental_notes_schema import KNOWN_PROCEDURES, DentalNote, plain_errors

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "dental-notes"

# the model is on this machine and nowhere else - see local_model.py. there
# is no remote fallback: a refused connection is OllamaUnreachable and the note
# goes to review.
from local_model import local_urlopen, is_local  # noqa: E402


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


CF_SHAPED = re.compile(r"\b[A-Za-z0-9]{16}\b")


def codes_in(text):
    """every codice fiscale written in full in a text, check letter right"""
    return sorted({normalize_cf(m) for m in CF_SHAPED.findall(text or "") if is_valid_cf(normalize_cf(m))})


def name_beside(text, cf):
    """the name written just before a code in the note, when the code's own six letters confirm it (P29 F-033) -
    "Mario Rossi RSSMRA85T10A562S" or "rossi mario rssmra85t10a562s"; never a guess"""
    at = (text or "").upper().find(cf)
    words = re.findall(r"[^\W\d_][^\W\d_'’]*", text[:at])[-4:] if at > 0 else []
    for start in range(len(words) - 1):
        cand = words[start:]
        for k in range(1, len(cand)):
            for surname, given in ((cand[k:], cand[:k]), (cand[:k], cand[k:])):
                if name_letters(" ".join(surname), " ".join(given)) == cf[:6]:
                    return " ".join(w.capitalize() for w in cand)
    return None


def parse_reply(reply, fallback_cf=None, source=None):
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
    # P29 F-033: the note writes the code in full but the model cut it short - the note's own code wins, when there
    # is exactly one; a missing name comes from beside it only when the code's letters confirm it
    found = codes_in(source)
    if len(found) == 1:
        if not is_valid_cf(normalize_cf(obj.get("codice_fiscale") or "")):
            obj["codice_fiscale"] = found[0]
        if not str(obj.get("patient_name") or "").strip():
            obj["patient_name"] = name_beside(source, found[0]) or obj.get("patient_name")
    if source is not None and not fallback_cf and not str(obj.get("patient_name") or "").strip():
        raise ValueError("the note's patient's name could not be read: write it next to the codice fiscale")
    if fallback_cf and not obj.get("codice_fiscale"):
        obj["codice_fiscale"] = fallback_cf
    try:
        note = DentalNote(**obj)
    except Exception as e:
        raise ValueError("model output failed schema validation: " + plain_errors(e))
    unknown = note.unknown_procedures()
    if unknown:
        print("unknown procedure code, needs review:", ", ".join(unknown))
    return note


def call_model(note, urlopen=local_urlopen):
    if not is_local(OLLAMA_URL):
        # checked before anything is opened, whoever supplied the opener
        raise OllamaUnreachable("refusing a model endpoint that is not on this machine")
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

    # 3b. a codice_fiscale that fails codice_fiscale.is_valid is flagged,
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

    # 6a. P29 F-018: a procedure counts only when the note says it - its code, its gloss or a synonym, inflected or not
    from dental_notes_schema import unsupported_procedures
    assert unsupported_procedures(["rct 46"], "controllo dente 46, nessuna novita") == ["rct 46"], "6a: rct invented"
    assert unsupported_procedures(["rct 46"], "46 devitalizzato oggi") == [], "6a: an inflected synonym"
    assert unsupported_procedures(["rct 46", "x-ray"], "root canal 46, radiografia") == [], "6a: gloss and synonym"
    assert unsupported_procedures(["prophy", "implant 36"], "igiene, impianto 36") == ["implant 36"], "6a: unknown code unsaid"
    # 6a2. P29 F-033: the model dropped the name and cut the code short, though the note writes both in full. the
    # note's own code (check letter right) replaces a broken one; a name is taken from beside it only when the code's
    # own letters confirm it - never a guess
    f033 = '{"patient_name": null, "codice_fiscale": "RSSMRA85T10", "procedures": [], "invoices": []}'
    note = parse_reply(f033, source="Mario Rossi RSSMRA85T10A562S, controllo 46")
    assert (note.patient_name, note.codice_fiscale) == ("Mario Rossi", "RSSMRA85T10A562S"), "6a2: F-033"
    note = parse_reply(f033, source="paziente rossi mario rssmra85t10a562s controllo")
    assert (note.patient_name, note.codice_fiscale) == ("Rossi Mario", "RSSMRA85T10A562S"), "6a2: surname first, lower case"
    try:
        parse_reply(f033, source="Luigi Bianchi RSSMRA85T10A562S, controllo 46")
        raise AssertionError("6a2: a name the code's letters do not confirm was taken")
    except ValueError as e:
        assert "patient's name" in str(e), f"6a2: the refusal does not say the name is missing: {e}"
    try:
        parse_reply('{"patient_name": "Mario Rossi", "codice_fiscale": "RSSMRA85", "procedures": []}',
                    source="RSSMRA85T10A562S and MRRS800010150100")
        raise AssertionError("6a2: one of two codes in the note was picked")
    except ValueError:
        pass
    # 6b. every code in the glossary is recognised, tooth number or not
    known = ('{"patient_name": "anna bianchi", "codice_fiscale": "BNCA850010150300", '
             '"procedures": ["rct 26", "x-ray 16", "prophy", "seal 16"], "invoices": [], '
             '"clinical_notes": "", "next_appointment": null}')
    assert parse_reply(known).unknown_procedures() == [], \
        "6b: glossary codes must not be flagged"

    # 6c. sigillatura is a sealant, not a filling - it has its own code so a
    # preventive procedure never lands on the record as a restorative one
    assert "seal" in KNOWN_PROCEDURES, "6c: seal should be in the glossary"
    assert KNOWN_PROCEDURES["seal"]["gloss"] != KNOWN_PROCEDURES["filling"]["gloss"], \
        "6c: seal and filling must stay distinct"
    assert "sigillatura" in KNOWN_PROCEDURES["seal"]["synonyms"], \
        "6c: sigillatura should map to seal"

    # 6e. the glossary shape the generator and the trainer both depend on. a flat
    # code -> string map cannot carry a second input term, which is what kept the
    # dataset english-only - so the shape itself is worth pinning.
    for code, entry in KNOWN_PROCEDURES.items():
        assert isinstance(entry, dict), f"6e: {code} should be an object, got {type(entry)}"
        assert isinstance(entry.get("gloss"), str) and entry["gloss"], \
            f"6e: {code} needs a non-empty gloss"
        assert isinstance(entry.get("synonyms"), list), f"6e: {code} needs a synonyms list"
        for syn in entry["synonyms"]:
            assert syn == syn.lower(), f"6e: synonym {syn!r} should be lowercase"

    # 6f. a synonym must not collide with a code or with another code's synonym -
    # an ambiguous surface term would train the model toward two different answers
    seen_terms = {}
    for code, entry in KNOWN_PROCEDURES.items():
        for term in [code] + entry["synonyms"]:
            assert term not in seen_terms or seen_terms[term] == code, \
                f"6f: {term!r} claimed by both {seen_terms.get(term)} and {code}"
            seen_terms[term] = code

    # 6g. an italian term in the model's OUTPUT means it failed to translate, so
    # it must flag - synonyms are input vocabulary, not valid output codes
    untranslated = ('{"patient_name": "anna bianchi", "codice_fiscale": "BNCA850010150300", '
                    '"procedures": ["otturazione 47"], "invoices": [], '
                    '"clinical_notes": "", "next_appointment": null}')
    assert parse_reply(untranslated).unknown_procedures() == ["otturazione"], \
        "6g: an untranslated italian term should be flagged, not accepted"

    # 6d. case and stray whitespace do not smuggle a code past the check
    messy = ('{"patient_name": "anna bianchi", "codice_fiscale": "BNCA850010150300", '
             '"procedures": ["  RCT 26", "Banana"], "invoices": [], '
             '"clinical_notes": "", "next_appointment": null}')
    assert parse_reply(messy).unknown_procedures() == ["banana"], \
        "6d: matching should be case- and whitespace-insensitive"

    # 7. the Modelfile SYSTEM prompt and the training notebook's SYSTEM_PROMPT
    # must be byte-identical. train and inference conditioning have to match, and
    # this pair has silently drifted twice - once dropping next_appointment from
    # the weights entirely. lives here because this is the file that owns the
    # model contract, and because run_selftests.sh runs it.
    root = Path(__file__).resolve().parent
    mf = re.search(r'SYSTEM """(.*?)"""', (root / "Modelfile").read_text(), re.S)
    assert mf, "7: no SYSTEM block in Modelfile"
    nb = json.loads((root / "notebooks" / "train_notes_lora.ipynb").read_text())
    nb_prompts = []
    for cell in nb["cells"]:
        found = re.search(r'SYSTEM_PROMPT = """(.*?)"""', "".join(cell["source"]), re.S)
        if found:
            nb_prompts.append(found.group(1))
    assert len(nb_prompts) == 1, f"7: expected 1 SYSTEM_PROMPT in the notebook, got {len(nb_prompts)}"
    assert mf.group(1) == nb_prompts[0], \
        "7: Modelfile SYSTEM and notebook SYSTEM_PROMPT have drifted apart"

    # 7b. the prompt has to name the italian mapping, or the training data teaches
    # a translation the inference prompt never asks for
    assert "otturazione -> filling" in mf.group(1), "7b: prompt should map otturazione"
    assert "sigillatura -> seal" in mf.group(1), "7b: prompt should map sigillatura"
    assert "English dental note" not in mf.group(1), \
        "7b: the english-only constraint should be gone"

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
