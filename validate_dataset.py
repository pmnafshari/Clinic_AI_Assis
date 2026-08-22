import json
import re
import sys
from datetime import date as date_type

from dental_notes_schema import KNOWN_PROCEDURES, DentalNote

RAW_FILE = "notes_raw_v2.jsonl"
TRAIN_FILE = "notes_train.jsonl"
TEST_FILE = "notes_test.jsonl"
# the italian adversarial cases (bf52ef1) live in their own file because the
# split below writes TEST_FILE with mode 'w'. they were hand-written to make the
# procedures gate able to FAIL - a resplit that silently dropped them would turn
# a green gate into evidence of nothing.
ADVERSARIAL_FILE = "notes_adversarial_it.jsonl"
GLOSSARY_FILE = "dental_shorthand_glossary.json"

# every italian surface the glossary knows must appear in the TRAINING set at
# least this many times. the bar is 1 on purpose: it is an invariant against a
# specific failure that already happened, not a quality target.
#
# what happened: notes_train.jsonl was generated at 435631c, before
# generate_dataset.py learned italian (6d74c3a) and before the glossary gained
# the terms (72e518b, 717e23c). nobody regenerated, so 9 of 10 italian synonyms
# sat at ZERO occurrences while the glossary, the modelfile prompt and the test
# set all claimed the model had been taught them. the gate could not see it -
# the model scored 0.97 on italian anyway, off the base model and the prompt,
# which is a far more fragile place for that competence to live.
MIN_TRAIN_OCCURRENCES = 1
# below this is legal but worth saying out loud
THIN_TRAIN_OCCURRENCES = 3
NEEDED = 180
TRAIN_COUNT = 150

CF_PATTERN = re.compile(r'^[A-Z]{4}[0-9]{12}$')
MONEY_PATTERN = re.compile(r'\d\s*(eur|euro|€|\$)', re.IGNORECASE)
MONEY_PATTERN2 = re.compile(r'(eur|euro|€|\$)\s*\d', re.IGNORECASE)
NEXT_APPT_FMT = re.compile(r'^\d+d$')
NEXT_APPT_SRC = re.compile(
    r'\d+\s*(week|weeks|month|months|day|days|wk|mo\b|d\b)',
    re.IGNORECASE,
)


def _date_in_raw(date_str, raw):
    if date_str in raw:
        return True
    try:
        d = date_type.fromisoformat(date_str)
        italian = f"{d.day:02d}/{d.month:02d}/{d.year}"
        dots = f"{d.day:02d}.{d.month:02d}.{d.year}"
        if italian in raw or dots in raw:
            return True
    except (ValueError, TypeError):
        pass
    return False


def _grounded(value, raw):
    # gold always carries the code ("filling 47"), but the note carries whatever
    # the dentist wrote ("otturazione 47"). requiring a literal match would drop
    # every italian sample as ungrounded - which is what kept this dataset
    # english-only. a code's own synonyms count as grounding for that code.
    if value.lower() in raw.lower():
        return True
    parts = value.split(" ", 1)
    entry = KNOWN_PROCEDURES.get(parts[0].lower())
    if not entry:
        return False
    rest = parts[1] if len(parts) > 1 else ""
    for syn in entry["synonyms"]:
        candidate = (syn + " " + rest).strip()
        if candidate.lower() in raw.lower():
            return True
    return False


def validate_sample(raw, gold):
    try:
        DentalNote(**gold)
    except Exception as e:
        return False, f"schema: {e}"

    cf = gold.get('codice_fiscale', '')

    if not CF_PATTERN.match(cf):
        return False, f"CF regex: {cf!r}"

    if cf not in raw:
        return False, f"CF not in raw: {cf!r}"

    name = gold.get('patient_name', '')
    if name.lower() not in raw.lower():
        return False, f"patient_name not in raw: {name!r}"

    phone = gold.get('phone')
    if phone and phone not in raw:
        return False, f"phone not in raw: {phone!r}"

    visit_date = gold.get('visit_date')
    if visit_date:
        d_str = visit_date if isinstance(visit_date, str) else str(visit_date)
        if not _date_in_raw(d_str, raw):
            return False, f"visit_date not in raw: {d_str!r}"

    for proc in gold.get('procedures', []):
        if not _grounded(proc, raw):
            return False, f"procedure not in raw: {proc!r}"
        if MONEY_PATTERN.search(proc) or MONEY_PATTERN2.search(proc):
            return False, f"procedure has money token: {proc!r}"

    for inv in gold.get('invoices', []):
        if isinstance(inv, dict):
            amount = inv.get('amount', 0)
            desc = inv.get('description', '')
        else:
            amount = inv.amount
            desc = inv.description
        amount_val = float(amount)
        if amount_val == int(amount_val):
            amount_str = str(int(amount_val))
        else:
            amount_str = str(amount_val)
        if amount_str not in raw:
            return False, f"invoice amount not in raw: {amount_str!r}"
        if not _grounded(desc, raw):
            return False, f"invoice desc not in raw: {desc!r}"

    next_appt = gold.get('next_appointment')
    if next_appt is not None:
        if not NEXT_APPT_FMT.match(next_appt):
            return False, f"next_appointment format: {next_appt!r}"
        if not NEXT_APPT_SRC.search(raw):
            return False, f"next_appointment no source phrase in raw"

    return True, "ok"


def load_valid(path):
    valid = []
    passed = 0
    failed = 0
    seen = set()
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                failed += 1
                continue
            raw = row.get('input', '')
            gold = row.get('output', {})
            if raw in seen:
                continue
            ok, reason = validate_sample(raw, gold)
            if not ok:
                print(f"  drop: {reason}")
                failed += 1
                continue
            seen.add(raw)
            valid.append(row)
            passed += 1
    return valid, passed, failed


def write_lines(path, rows):
    with open(path, 'w') as f:
        for row in rows:
            f.write(json.dumps(row) + '\n')


def synonym_coverage():
    # returns (missing, thin) - synonym -> count, for surfaces the training set
    # under-represents. pure counting, no I/O beyond the three files.
    with open(GLOSSARY_FILE) as f:
        glossary = json.load(f)
    train = open(TRAIN_FILE).read().lower()

    missing, thin = {}, {}
    for code, entry in glossary.items():
        for synonym in entry.get("synonyms", []):
            count = train.count(synonym.lower())
            if count < MIN_TRAIN_OCCURRENCES:
                missing[synonym] = count
            elif count < THIN_TRAIN_OCCURRENCES:
                thin[synonym] = count
    return missing, thin


def selftest():
    # 1. no glossary synonym may be absent from the training set
    missing, thin = synonym_coverage()
    assert not missing, (
        f"1: these italian surfaces are in the glossary but absent from {TRAIN_FILE}: "
        f"{sorted(missing)} - regenerate with generate_dataset.py, do not hand-edit"
    )

    # 2. the adversarial cases exist and are still the tail of the test set.
    # they are the only cases that let the procedures gate FAIL, so losing them
    # turns a green gate into evidence of nothing.
    with open(ADVERSARIAL_FILE) as f:
        adversarial = [json.loads(line) for line in f if line.strip()]
    assert adversarial, f"2: {ADVERSARIAL_FILE} is empty"
    with open(TEST_FILE) as f:
        test_rows = [json.loads(line) for line in f if line.strip()]
    assert test_rows[-len(adversarial):] == adversarial, \
        f"2: the tail of {TEST_FILE} is not {ADVERSARIAL_FILE} - a resplit dropped them"

    # 3. and they still carry the italian the gate needs to be able to fail on
    blob = json.dumps(adversarial, ensure_ascii=False).lower()
    for term in ("otturazione", "estrazione", "radiografia", "devitalizzazione"):
        assert term in blob, f"3: the adversarial set lost {term}"

    if thin:
        print(f"  thin (legal, but <{THIN_TRAIN_OCCURRENCES} in training): "
              f"{dict(sorted(thin.items()))}")
    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    try:
        valid, passed, failed = load_valid(RAW_FILE)
    except FileNotFoundError:
        print("no", RAW_FILE, "- run generate_dataset.py first")
        sys.exit(1)

    print(f"passed: {passed}  failed/dropped: {failed}")

    if len(valid) < NEEDED:
        print(f"only {len(valid)} valid - need {NEEDED}; re-run generate_dataset.py")
        sys.exit(1)

    train = valid[:TRAIN_COUNT]
    test = valid[TRAIN_COUNT:NEEDED]

    # re-attach the hand-written adversarial cases, always. not optional: the
    # gate is only meaningful while these are in the test set.
    try:
        with open(ADVERSARIAL_FILE) as f:
            adversarial = [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        print(f"REFUSING: {ADVERSARIAL_FILE} is missing - the italian adversarial")
        print("cases are what let the procedures gate fail. restore it before splitting.")
        sys.exit(1)
    if not adversarial:
        print(f"REFUSING: {ADVERSARIAL_FILE} is empty")
        sys.exit(1)
    test = test + adversarial

    write_lines(TRAIN_FILE, train)
    write_lines(TEST_FILE, test)
    print(f"wrote {len(train)} to {TRAIN_FILE} and {len(test)} to {TEST_FILE}"
          f" ({len(adversarial)} adversarial cases re-attached)")

    missing, thin = synonym_coverage()
    if missing:
        print(f"WARNING: italian surfaces absent from {TRAIN_FILE}: {sorted(missing)}")
    if thin:
        print(f"note: thinly covered in training: {dict(sorted(thin.items()))}")


if __name__ == "__main__":
    main()
