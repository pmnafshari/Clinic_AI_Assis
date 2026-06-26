import json
import re
import sys
from datetime import date as date_type

from dental_notes_schema import DentalNote

RAW_FILE = "notes_raw_v2.jsonl"
TRAIN_FILE = "notes_train.jsonl"
TEST_FILE = "notes_test.jsonl"
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
        if proc.lower() not in raw.lower():
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
        if desc.lower() not in raw.lower():
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


def main():
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
    write_lines(TRAIN_FILE, train)
    write_lines(TEST_FILE, test)
    print(f"wrote {len(train)} to {TRAIN_FILE} and {len(test)} to {TEST_FILE}")


if __name__ == "__main__":
    main()
