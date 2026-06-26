import json
import re
import sys

from extract_note import OllamaUnreachable, extract_note

TEST_FILE = "notes_test.jsonl"
THRESHOLD = 0.85
CF_RE = re.compile(r'^[A-Z]{4}[0-9]{12}$')

GATE_FIELDS = [
    "patient_name",
    "codice_fiscale",
    "phone",
    "visit_date",
    "procedures",
    "invoices",
    "next_appointment",
]


def norm_str(v):
    if v is None:
        return ""
    return " ".join(str(v).split()).lower()


def token_f1(pred, gold):
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
        ps = {norm_str(x) for x in pred or []}
        gs = {norm_str(x) for x in gold or []}
        if not ps and not gs:
            return True
        if not ps or not gs:
            return False
        inter = len(ps & gs)
        f1 = 2 * inter / (len(ps) + len(gs))
        return f1 >= 0.8

    if name == "invoices":
        pred_list = list(pred or [])
        gold_list = list(gold or [])
        if not pred_list and not gold_list:
            return True
        if not pred_list or not gold_list:
            return False
        matched = 0
        used = set()
        for g_item in gold_list:
            if isinstance(g_item, dict):
                g_amt = g_item.get("amount")
                g_desc = g_item.get("description", "")
            else:
                g_amt = getattr(g_item, "amount", None)
                g_desc = getattr(g_item, "description", "")
            try:
                g_amt = float(g_amt)
            except (TypeError, ValueError):
                g_amt = None
            for pi, p_item in enumerate(pred_list):
                if pi in used:
                    continue
                if isinstance(p_item, dict):
                    p_amt = p_item.get("amount")
                    p_desc = p_item.get("description", "")
                else:
                    p_amt = getattr(p_item, "amount", None)
                    p_desc = getattr(p_item, "description", "")
                try:
                    p_amt = float(p_amt)
                except (TypeError, ValueError):
                    p_amt = None
                if p_amt == g_amt and token_f1(p_desc, g_desc) >= 0.8:
                    matched += 1
                    used.add(pi)
                    break
        f1 = 2 * matched / (len(pred_list) + len(gold_list))
        return f1 >= 0.8

    if name == "codice_fiscale":
        pn = norm_str(pred)
        gn = norm_str(gold)
        return pn == gn and bool(CF_RE.match(gold or ""))

    # next_appointment and all other scalar fields
    return norm_str(pred) == norm_str(gold)


def score_note(pred, gold):
    hits = sum(1 for f in GATE_FIELDS if field_match(f, pred.get(f), gold.get(f)))
    return hits / len(GATE_FIELDS)


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
    field_hits = {f: 0 for f in GATE_FIELDS}
    cn_total = 0.0

    for row in rows:
        gold = row["output"]
        try:
            result = extract_note(row["input"])
            pred = result.model_dump(mode="json")
        except OllamaUnreachable as e:
            print(e)
            sys.exit(1)
        except ValueError:
            pred = {}
        n += 1
        s = score_note(pred, gold)
        total += s
        print("note", n, "score", round(s, 3))
        for f in GATE_FIELDS:
            if field_match(f, pred.get(f), gold.get(f)):
                field_hits[f] += 1
        cn_total += token_f1(pred.get("clinical_notes"), gold.get("clinical_notes"))

    avg = total / n if n else 0.0
    print("avg field accuracy:", round(avg, 2), "over", n, "notes")
    print("\nper-field pass rate:")
    for f in GATE_FIELDS:
        rate = round(field_hits[f] / n, 2) if n else 0.0
        print(" ", f, rate)
    cn_avg = cn_total / n if n else 0.0
    print("clinical_notes overlap (not gated):", round(cn_avg, 2))
    sys.exit(0 if avg >= THRESHOLD else 1)


if __name__ == "__main__":
    main()
