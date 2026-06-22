import json
import sys
import urllib.error
import urllib.request

GLOSSARY_FILE = "dental_shorthand_glossary.json"
RAW_FILE = "notes_raw.jsonl"
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "llama3.2"
TARGET = 180

FIELDS = [
    "patient_name",
    "codice_fiscale",
    "phone",
    "visit_date",
    "procedures",
    "invoices",
    "notes_text",
]


def load_glossary():
    with open(GLOSSARY_FILE) as f:
        return json.load(f)


def build_instruction(glossary):
    lines = ["Read a messy English dental note and return clean JSON."]
    lines.append("Use only what the note says. Do not invent missing data.")
    lines.append("JSON keys: " + ", ".join(FIELDS) + ".")
    lines.append("Shorthand:")
    for token, meaning in glossary.items():
        lines.append("  " + token + " = " + meaning)
    return "\n".join(lines)


def build_teacher_prompt(glossary):
    shorthand = ", ".join(glossary.keys())
    return (
        "You write training data for a dental notes reader.\n"
        "Invent ONE short, realistic, messy note that a dentist would jot about a "
        "SINGLE patient's visit. Write it naturally in shorthand - do NOT list every "
        "abbreviation. Use only 1 to 3 shorthand tokens that fit the visit, from: "
        + shorthand + ".\n"
        "Use a fake Italian name and a realistic codice fiscale (16 letters and "
        "digits, like RSSMRA80A01H501U). Vary the patient and visit each time. "
        "Sometimes include a phone, a date, or a charge; often leave them out.\n\n"
        "Return ONLY a JSON object with two keys:\n"
        '  "note": the messy note text (string)\n'
        '  "data": an object with keys: ' + ", ".join(FIELDS) + "\n"
        "Rules for data:\n"
        "- copy patient_name and codice_fiscale from the note\n"
        "- phone and visit_date are null unless the note states them\n"
        "- procedures: list of the treatments mentioned (short strings)\n"
        "- invoices: list of {description, amount} ONLY if the note states a price; "
        "else an empty list\n"
        "- notes_text: a single plain string of the free-text remarks (never a list)\n"
        "- never invent values the note does not contain\n\n"
        "Two examples of the exact format:\n"
        '{"note": "anna bianchi RSSBNC85M41F205K, ext 38, scaling, abx given, fu 1wk", '
        '"data": {"patient_name": "anna bianchi", "codice_fiscale": "RSSBNC85M41F205K", '
        '"phone": null, "visit_date": null, "procedures": ["ext 38", "scaling"], '
        '"invoices": [], "notes_text": "abx given, follow up in 1 week"}}\n'
        '{"note": "Mario Rossi MRARSS80A01H501U tel 333 1234567, rct 26 done, comp 27, paid 150 eur", '
        '"data": {"patient_name": "Mario Rossi", "codice_fiscale": "MRARSS80A01H501U", '
        '"phone": "333 1234567", "visit_date": null, "procedures": ["rct 26", "comp 27"], '
        '"invoices": [{"description": "rct 26", "amount": 150}], "notes_text": "rct 26 done, composite filling 27"}}\n\n'
        "Now invent a new, different one."
    )


def call_ollama(prompt):
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.8},
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(OLLAMA_URL, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.load(resp)
        return body.get("response", "")
    except urllib.error.URLError:
        print("Ollama not reachable at localhost:11434 - start it with: ollama run llama3.2")
        sys.exit(1)


def extract_json(text):
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def count_lines(path):
    try:
        with open(path) as f:
            return sum(1 for line in f if line.strip())
    except FileNotFoundError:
        return 0


def main():
    target = int(sys.argv[1]) if len(sys.argv) > 1 else TARGET
    glossary = load_glossary()
    instruction = build_instruction(glossary)
    teacher_prompt = build_teacher_prompt(glossary)

    done = count_lines(RAW_FILE)
    print("already have", done, "examples, target", target)

    with open(RAW_FILE, "a") as out:
        while done < target:
            reply = call_ollama(teacher_prompt)
            obj = extract_json(reply)
            if obj is None or "note" not in obj or "data" not in obj:
                print("skipped a bad reply")
                continue
            line = {"instruction": instruction, "input": obj["note"], "output": obj["data"]}
            out.write(json.dumps(line) + "\n")
            out.flush()
            done += 1
            print("generated", done, "/", target)

    print("done -", done, "examples in", RAW_FILE)


if __name__ == "__main__":
    main()
