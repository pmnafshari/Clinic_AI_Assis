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
        "Invent ONE realistic messy English dental clinic note, one or two lines, "
        "written in dentist shorthand. Use a fake Italian patient name and a fake "
        "codice fiscale. Use some of these shorthand tokens: " + shorthand + ".\n"
        "Then give the matching clean data.\n"
        "Return ONLY a JSON object with two keys:\n"
        '  "note": the messy note text (string)\n'
        '  "data": an object with these keys: ' + ", ".join(FIELDS) + "\n"
        "data rules: phone and visit_date may be null if the note has none. "
        "procedures is a list of strings. invoices is a list of objects with "
        "description (string) and amount (number). notes_text is the free text. "
        "Only include values the note actually mentions."
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
