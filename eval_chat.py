import json
import sqlite3
import sys

from patient_app.chat import answer_question

TEST_FILE = "chat_test.jsonl"
THRESHOLD = 0.85
# an "answer" case is exactly DEF-1's target - the model was handed the right,
# scoped context and has to restate it. gated at 1.0 on its own, same reason
# eval_notes.py gates procedures on its own: a single dropped appointment or
# invoice is a patient-safety-adjacent failure (SC1), not a rounding error an
# aggregate average can hide.
ANSWER_THRESHOLD = 1.0


def build_db(patients):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE patients (
            codice_fiscale TEXT PRIMARY KEY,
            patient_name TEXT NOT NULL,
            phone TEXT
        );
        CREATE TABLE visits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            codice_fiscale TEXT NOT NULL,
            visit_date TEXT,
            procedures TEXT,
            clinical_notes TEXT,
            next_appointment TEXT,
            source_path TEXT UNIQUE NOT NULL
        );
        CREATE TABLE invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            codice_fiscale TEXT NOT NULL,
            visit_id INTEGER NOT NULL,
            line_index INTEGER NOT NULL,
            amount REAL NOT NULL,
            description TEXT
        );
        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            username TEXT NOT NULL,
            role TEXT NOT NULL,
            action TEXT NOT NULL,
            target TEXT,
            allowed INTEGER NOT NULL,
            ip TEXT
        );
    """)
    for i, p in enumerate(patients):
        conn.execute(
            "INSERT INTO patients (codice_fiscale, patient_name, phone) VALUES (?, ?, ?)",
            (p["cf"], p["name"], p["phone"]),
        )
        if p.get("visit_date") is None:
            continue
        source_path = f"eval/{i}.json"
        conn.execute(
            "INSERT INTO visits (codice_fiscale, visit_date, procedures, clinical_notes,"
            " next_appointment, source_path) VALUES (?, ?, ?, ?, ?, ?)",
            (p["cf"], p["visit_date"], json.dumps(p["procedures"]), "", p["next_appointment"],
             source_path),
        )
        if p.get("invoice_amount") is None:
            continue
        visit_id = conn.execute(
            "SELECT id FROM visits WHERE source_path = ?", (source_path,)
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO invoices (codice_fiscale, visit_id, line_index, amount, description)"
            " VALUES (?, ?, 0, ?, ?)",
            (p["cf"], visit_id, p["invoice_amount"], p["invoice_description"]),
        )
    conn.commit()
    return conn


def load_tests(path):
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


IT_MONTHS = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
             "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"]
EN_MONTHS = ["january", "february", "march", "april", "may", "june",
             "july", "august", "september", "october", "november", "december"]


def date_variants(iso_date, lang):
    # the model may restate a date faithfully in more than one surface form
    # (dd/mm/yyyy, no leading zeros, or the long form) - all count as
    # correct. generated from the one canonical iso value in the fixture,
    # not hand-listed per case.
    year, month, day = iso_date.split("-")
    year, month, day = int(year), int(month), int(day)
    months = EN_MONTHS if lang == "en" else IT_MONTHS
    month_name = months[month - 1]
    return [
        f"{day:02d}/{month:02d}/{year}",
        f"{day}/{month}/{year}",
        f"{day} {month_name} {year}",
    ]


def amount_variants(amount):
    # same idea for money - comma or dot decimal, with or without the euro
    # sign, generated from the one canonical float in the fixture.
    dot = f"{amount:.2f}"
    comma = dot.replace(".", ",")
    variants = []
    for v in (dot, comma):
        variants.append(v)
        variants.append(f"€{v}")
        variants.append(f"€ {v}")
    return variants


def expected_variants(item, lang):
    # a plain string is a bare identifier (tooth number, phone number) -
    # those have exactly one correct rendering and stay exact-match. a dict
    # names which canonical value to expand into accepted renderings.
    if isinstance(item, str):
        return [item]
    if "date" in item:
        return date_variants(item["date"], lang)
    if "amount" in item:
        return amount_variants(item["amount"])
    raise ValueError(f"unknown expect_contains item: {item!r}")


def score_case(result, case):
    expect_state = case["expect_state"]
    if result["state"] != expect_state:
        return False, f"state was {result['state']!r}, expected {expect_state!r}"
    if expect_state != "answer":
        return True, "ok"
    body = (result["body"] or "").lower()
    lang = case.get("lang", "it")
    missing = []
    for item in case["expect_contains"]:
        variants = expected_variants(item, lang)
        if not any(v.lower() in body for v in variants):
            missing.append(item)
    if missing:
        return False, f"missing {missing} in body {result['body']!r}"
    return True, "ok"


def selftest():
    # 1. the real bruno_visits body from the pre-fix baseline restated the
    # seeded date faithfully, just in long form instead of dd/mm/yyyy - the
    # date component must now pass.
    result = {"state": "answer",
              "body": "Hai fatto una visita di controllo alle denti il 19 maggio 2026."}
    case = {"expect_state": "answer", "lang": "it",
            "expect_contains": [{"date": "2026-05-19"}]}
    ok, detail = score_case(result, case)
    assert ok, f"1: faithfully restated date should pass, got: {detail}"

    # 2. an invented date must still fail - a scorer that accepts everything
    # is worse than the one it replaces.
    result = {"state": "answer",
              "body": "Hai fatto una visita di controllo il 20/02/2026."}
    case = {"expect_state": "answer", "lang": "it",
            "expect_contains": [{"date": "2026-03-04"}]}
    ok, detail = score_case(result, case)
    assert not ok, "2: invented date must still fail"

    # 3. euro sign plus comma decimal must pass against a plain float.
    result = {"state": "answer", "body": "Devi pagare € 120,50."}
    case = {"expect_state": "answer", "lang": "it",
            "expect_contains": [{"amount": 120.5}]}
    ok, detail = score_case(result, case)
    assert ok, f"3: euro/comma amount should pass, got: {detail}"

    # 4. a wrong amount must still fail.
    result = {"state": "answer", "body": "Devi pagare 90,00."}
    case = {"expect_state": "answer", "lang": "it",
            "expect_contains": [{"amount": 120.5}]}
    ok, detail = score_case(result, case)
    assert not ok, "4: wrong amount must still fail"

    # 5. bare identifiers (tooth number, phone) stay exact-match - a
    # close-but-wrong tooth number must still fail.
    result = {"state": "answer", "body": "hai fatto un intervento sul dente 47."}
    case = {"expect_state": "answer", "lang": "it", "expect_contains": ["46"]}
    ok, detail = score_case(result, case)
    assert not ok, "5: wrong tooth number must still fail"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return

    rows = load_tests(TEST_FILE)

    patients = {}
    for row in rows:
        patients.setdefault(row["patient"]["cf"], row["patient"])
    conn = build_db(list(patients.values()))

    total = 0
    passed = 0
    answer_total = 0
    answer_passed = 0

    for row in rows:
        result = answer_question(row["question"], row["patient"]["cf"], conn, row["lang"])
        ok, detail = score_case(result, row)
        total += 1
        passed += int(ok)
        if row["expect_state"] == "answer":
            answer_total += 1
            answer_passed += int(ok)
        print(row["id"], "PASS" if ok else "FAIL", "-", detail)

    rate = passed / total if total else 0.0
    answer_rate = answer_passed / answer_total if answer_total else 0.0
    print("\noverall pass rate:", round(rate, 3), "over", total, "cases")
    print("answer-case fidelity rate:", round(answer_rate, 3), "over", answer_total, "cases")

    gate_passed = rate >= THRESHOLD and answer_rate >= ANSWER_THRESHOLD
    if rate >= THRESHOLD and answer_rate < ANSWER_THRESHOLD:
        print(f"\nFAIL: answer-case fidelity {round(answer_rate, 3)} below {ANSWER_THRESHOLD}"
              " (aggregate passed but a scoped answer case dropped or denied real data - DEF-1)")
    sys.exit(0 if gate_passed else 1)


if __name__ == "__main__":
    main()
