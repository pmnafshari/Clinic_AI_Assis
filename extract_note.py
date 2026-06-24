import json
import sys
import urllib.error
import urllib.request

from dental_notes_schema import DentalNote

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


def parse_reply(reply):
    # reply -> validated DentalNote. Raises ValueError when the output is not
    # schema-valid so a half-formed or wrong-shape record never passes silently.
    obj = extract_json(reply)
    if obj is None:
        raise ValueError("model did not return valid JSON")
    try:
        return DentalNote(**obj)
    except Exception as e:
        raise ValueError("model output failed schema validation: " + str(e))


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
    except urllib.error.URLError:
        raise OllamaUnreachable("Ollama not reachable - run: ollama run dental-notes")
    return body.get("response", "")


def extract_note(note):
    return parse_reply(call_model(note))


def selftest():
    # 1. valid output validates
    good = ('{"patient_name": "mario rossi", "codice_fiscale": "MRARSS80A01H501U", '
            '"phone": null, "visit_date": null, "procedures": ["rct 26"], '
            '"invoices": [], "notes_text": "rct done"}')
    note = parse_reply(good)
    assert note.patient_name == "mario rossi"
    assert note.phone is None

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

    # 4. semantic hallucination: the note has no phone but the output invents one.
    # Pydantic CANNOT catch this - the value has the right type, so it validates.
    # This is intentional and out of scope per 02-CONTEXT.md: the defenses are the
    # prompt-level guard (Modelfile SYSTEM) plus the aggregate 85% eval gate, NOT
    # per-call rejection. Asserting it validates documents the limitation so no one
    # assumes this self-test catches semantic hallucination.
    hallucinated = ('{"patient_name": "luca verdi", "codice_fiscale": "VRDLCU90A01H501U", '
                    '"phone": "333 0000000", "visit_date": null, "procedures": [], '
                    '"invoices": [], "notes_text": ""}')
    note = parse_reply(hallucinated)
    assert note.phone == "333 0000000"  # passes validation despite being invented

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
