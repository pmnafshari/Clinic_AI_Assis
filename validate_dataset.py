import json
import sys

from dental_notes_schema import DentalNote

RAW_FILE = "notes_raw.jsonl"
TRAIN_FILE = "notes_dataset.jsonl"
TEST_FILE = "notes_test.jsonl"
NEEDED = 180
TRAIN_COUNT = 150


def load_valid(path):
    valid = []
    passed = 0
    failed = 0
    seen_inputs = set()
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
            output = row.get("output", {})
            if not output.get("codice_fiscale"):
                failed += 1
                continue
            try:
                DentalNote(**output)
            except Exception:
                failed += 1
                continue
            note = row.get("input", "")
            if note in seen_inputs:
                continue
            seen_inputs.add(note)
            valid.append(row)
            passed += 1
    return valid, passed, failed


def write_lines(path, rows):
    with open(path, "w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def main():
    try:
        valid, passed, failed = load_valid(RAW_FILE)
    except FileNotFoundError:
        print("no", RAW_FILE, "- run generate_dataset.py first")
        sys.exit(1)

    print("passed:", passed, "failed/dropped:", failed)

    if len(valid) < NEEDED:
        print("only", len(valid), "valid examples - need", NEEDED)
        print("re-run generate_dataset.py to add more, then run this again")
        sys.exit(1)

    train = valid[:TRAIN_COUNT]
    test = valid[TRAIN_COUNT:NEEDED]
    write_lines(TRAIN_FILE, train)
    write_lines(TEST_FILE, test)
    print("wrote", len(train), "to", TRAIN_FILE, "and", len(test), "to", TEST_FILE)


if __name__ == "__main__":
    main()
