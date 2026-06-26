import json
import random
import sys
import urllib.error
import urllib.request

from cf_generator import make_cf, seed_cf
from validate_dataset import validate_sample

GLOSSARY_FILE = "dental_shorthand_glossary.json"
OUT_FILE = "notes_raw_v2.jsonl"
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "llama3.2"
TARGET = 180
SEED = 42

FIRST_NAMES = [
    "Marco", "Luca", "Matteo", "Andrea", "Lorenzo", "Alessandro", "Davide",
    "Giulia", "Chiara", "Sara", "Martina", "Federica", "Valentina", "Alessia",
    "Mario", "Gianni", "Stefano", "Roberto", "Antonio", "Giovanni",
    "Laura", "Elena", "Monica", "Paola", "Silvia", "Cristina", "Anna",
    "Paolo", "Claudio", "Francesco",
]

LAST_NAMES = [
    "Rossi", "Ferrari", "Esposito", "Bianchi", "Romano", "Ricci", "Marino",
    "Greco", "Bruno", "Gallo", "Conti", "Mancini", "Costa", "Giordano",
    "Rizzo", "Lombardi", "Moretti", "Barbieri", "Fontana", "Santoro",
]

TOOTH_NUMS = list(range(11, 29)) + list(range(31, 49))
PHONE_PREFIXES = ["333", "347", "366", "338", "345", "380", "340", "328"]
NEXT_APPT_OPTIONS = ["7d", "14d", "21d", "30d"]
NEXT_APPT_RAW = {"7d": "1wk", "14d": "2wk", "21d": "3wk", "30d": "1mo"}


def load_glossary():
    with open(GLOSSARY_FILE) as f:
        return json.load(f)


def pick_profile(rng):
    p = {}
    p["empty_proc"] = rng.random() < 0.22
    p["multi_proc"] = (not p["empty_proc"]) and rng.random() < 0.40
    p["empty_inv"] = rng.random() < 0.28
    p["multi_inv"] = (not p["empty_inv"]) and rng.random() < 0.20
    p["null_next"] = rng.random() < 0.32
    p["null_phone"] = rng.random() < 0.40
    p["empty_notes"] = rng.random() < 0.25
    p["messy_case"] = rng.random() < 0.25
    p["include_date"] = rng.random() < 0.50
    return p


def build_structured(rng, glossary, profile):
    # phone - always consume same rng calls for determinism
    prefix = rng.choice(PHONE_PREFIXES)
    num = rng.randint(1000000, 9999999)
    phone = f"{prefix} {num}" if not profile["null_phone"] else None

    # date - always consume same rng calls
    year = rng.randint(2022, 2024)
    month = rng.randint(1, 12)
    day = rng.randint(1, 28)
    visit_date = f"{year}-{month:02d}-{day:02d}" if profile["include_date"] else None

    # procedure tokens - exclude "fu" (follow-up, not a procedure)
    tokens = [k for k in glossary.keys() if k != "fu"]
    selected = rng.sample(tokens, min(3, len(tokens)))
    tooth1 = rng.choice(TOOTH_NUMS)
    tooth2 = rng.choice(TOOTH_NUMS)

    if profile["empty_proc"]:
        procedures = []
    elif profile["multi_proc"]:
        procedures = [f"{selected[0]} {tooth1}", f"{selected[1]} {tooth2}"]
    else:
        procedures = [f"{selected[0]} {tooth1}"]

    # invoice amounts - always consume same rng calls
    amount1 = rng.randint(3, 20) * 10
    amount2 = rng.randint(3, 20) * 10

    if profile["empty_inv"] or not procedures:
        invoices = []
    elif profile["multi_inv"] and len(procedures) >= 2:
        invoices = [
            {"amount": float(amount1), "description": procedures[0]},
            {"amount": float(amount2), "description": procedures[1]},
        ]
    else:
        invoices = [{"amount": float(amount1), "description": procedures[0]}]

    # next appointment - always consume same rng calls
    appt_choice = rng.choice(NEXT_APPT_OPTIONS)
    next_appt = appt_choice if not profile["null_next"] else None

    return phone, visit_date, procedures, invoices, next_appt


def build_template(name, cf, phone, visit_date, procedures, invoices, next_appt):
    parts = [f"{name} {cf}"]
    if phone:
        parts.append(f"tel {phone}")
    if visit_date:
        parts.append(visit_date)
    if procedures:
        parts.append(", ".join(procedures))
    if invoices:
        for inv in invoices:
            parts.append(f"paid {int(inv['amount'])} for {inv['description']}")
    if next_appt:
        parts.append(f"fu {NEXT_APPT_RAW[next_appt]}")
    return ", ".join(parts)


def build_messify_prompt(template, profile):
    lines = [
        "Rewrite this dental note as a natural, messy handwritten note.",
        "You MUST keep ALL specific values EXACTLY as they appear:",
        "patient name, CF code, phone number, tooth numbers, treatment codes, prices.",
        "You may add brief clinical observations (1 sentence) or reorder items.",
    ]
    if profile["messy_case"]:
        lines.append("Write in lowercase or mixed case.")
    if profile["empty_notes"]:
        lines.append("Keep it brief - no extra remarks.")
    lines.extend([
        "",
        f"Input: {template}",
        "",
        'Return ONLY JSON: {"note": "...", "clinical_notes": "..."}',
        "clinical_notes: a short clinical remark if you added one, else empty string.",
    ])
    return "\n".join(lines)


def call_ollama(prompt):
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.9},
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        OLLAMA_URL, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.load(resp)
        return body.get("response", "")
    except urllib.error.URLError:
        print("Ollama not reachable - start with: ollama run llama3.2")
        sys.exit(1)


def extract_json(text):
    start = text.find("{")
    if start == -1:
        return None
    # try from last } backwards to handle double-closing braces
    for end in range(len(text) - 1, start - 1, -1):
        if text[end] == "}":
            try:
                return json.loads(text[start: end + 1])
            except json.JSONDecodeError:
                pass
    return None


def main():
    target = int(sys.argv[1]) if len(sys.argv) > 1 else TARGET
    glossary = load_glossary()

    rng = random.Random(SEED)
    seed_cf(SEED)

    done = 0
    print(f"generating {target} samples fresh (seed={SEED})")

    with open(OUT_FILE, "w") as out:
        while done < target:
            first = rng.choice(FIRST_NAMES)
            last = rng.choice(LAST_NAMES)
            name = first + " " + last
            cf = make_cf(first, last)
            profile = pick_profile(rng)

            phone, visit_date, procedures, invoices, next_appt = build_structured(
                rng, glossary, profile
            )

            template = build_template(name, cf, phone, visit_date, procedures, invoices, next_appt)

            prompt = build_messify_prompt(template, profile)
            reply = call_ollama(prompt)
            obj = extract_json(reply)

            if obj and "note" in obj and isinstance(obj["note"], str):
                raw = obj["note"]
                clinical_notes = obj.get("clinical_notes") or ""
                if not isinstance(clinical_notes, str):
                    clinical_notes = ""
            else:
                raw = template
                clinical_notes = ""

            # ensure CF is in raw
            if cf not in raw:
                raw = template
                clinical_notes = ""

            gold = {
                "patient_name": name,
                "codice_fiscale": cf,
                "phone": phone,
                "visit_date": visit_date,
                "procedures": procedures,
                "invoices": invoices,
                "clinical_notes": clinical_notes,
                "next_appointment": next_appt,
            }

            ok, reason = validate_sample(raw, gold)
            if not ok:
                # LLM broke grounding - fall back to template
                raw = template
                gold["clinical_notes"] = ""
                ok, reason = validate_sample(raw, gold)
                if not ok:
                    print(f"  template failed: {reason}, skipping")
                    continue

            row = {"input": raw, "output": gold}
            out.write(json.dumps(row) + "\n")
            out.flush()
            done += 1
            print(f"generated {done}/{target}")

    print(f"done - {done} examples in {OUT_FILE}")


if __name__ == "__main__":
    main()
