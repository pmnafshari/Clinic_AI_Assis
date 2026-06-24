import json
import sys

from extract_note import OllamaUnreachable, extract_note

TEST_FILE = "notes_test.jsonl"
THRESHOLD = 0.85

FIELDS = [
    "patient_name",
    "codice_fiscale",
    "phone",
    "visit_date",
    "procedures",
    "invoices",
    "notes_text",
]


def norm_str(v):
    if v is None:
        return ""
    return " ".join(str(v).split()).lower()


def norm_invoices(items):
    out = set()
    for it in items or []:
        if isinstance(it, dict):
            desc = norm_str(it.get("description"))
            amt = it.get("amount")
        else:
            desc = norm_str(getattr(it, "description", None))
            amt = getattr(it, "amount", None)
        try:
            amt = float(amt)
        except (TypeError, ValueError):
            amt = None
        out.add((desc, amt))
    return out


NOTES_TEXT_THRESHOLD = 0.6


def token_overlap(pred, gold):
    # F1 over word tokens. notes_text is a free-form summary, so exact match is
    # too strict - we accept a prediction that shares most words with the gold.
    p = set(norm_str(pred).split())
    g = set(norm_str(gold).split())
    if not p and not g:
        return 1.0
    if not p or not g:
        return 0.0
    inter = len(p & g)
    if inter == 0:
        return 0.0
    precision = inter / len(p)
    recall = inter / len(g)
    return 2 * precision * recall / (precision + recall)


def field_match(name, pred, gold):
    if name == "procedures":
        return {norm_str(x) for x in pred or []} == {norm_str(x) for x in gold or []}
    if name == "invoices":
        return norm_invoices(pred) == norm_invoices(gold)
    if name == "notes_text":
        return token_overlap(pred, gold) >= NOTES_TEXT_THRESHOLD
    return norm_str(pred) == norm_str(gold)


def score_note(pred, gold):
    hits = 0
    for f in FIELDS:
        if field_match(f, pred.get(f), gold.get(f)):
            hits += 1
    return hits / len(FIELDS)


def load_tests(path):
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def main():
    rows = load_tests(TEST_FILE)
    total = 0.0
    n = 0
    for row in rows:
        gold = row["output"]
        try:
            result = extract_note(row["input"])
            pred = result.model_dump(mode="json")
        except OllamaUnreachable as e:
            print(e)
            sys.exit(1)
        except ValueError:
            pred = {}  # rejected output scores zero for this note
        n += 1
        s = score_note(pred, gold)
        total += s
        print("note", n, "score", round(s, 3))

    avg = total / n if n else 0.0
    print("avg field accuracy: " + format(avg, ".2f") + " over " + str(n) + " notes")
    sys.exit(0 if avg >= THRESHOLD else 1)


if __name__ == "__main__":
    main()
