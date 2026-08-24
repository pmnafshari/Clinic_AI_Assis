import json
import sqlite3
import sys

import patient_accessor

from patient_app.chat import answer_question

TEST_FILE = "chat_test.jsonl"
THRESHOLD = 0.85
# an "answer" case is exactly DEF-1's target - the model was handed the right,
# scoped context and has to restate it. gated at 1.0 on its own, same reason
# eval_notes.py gates procedures on its own: a single dropped appointment or
# invoice is a patient-safety-adjacent failure (SC1), not a rounding error an
# aggregate average can hide.
ANSWER_THRESHOLD = 1.0


def patient_visits(p):
    # a fixture declares its visits one of two ways: the original flat shape,
    # which is one visit and at most one invoice spread across top-level keys,
    # or a "visits" list for the multi-visit patients. both normalise to the
    # same list here so build_db has a single insert path and the single-visit
    # cases keep producing exactly the rows they produced before.
    if "visits" in p:
        return [
            {
                "visit_date": v["visit_date"],
                "procedures": v["procedures"],
                "next_appointment": v["next_appointment"],
                "invoices": v.get("invoices") or [],
            }
            for v in p["visits"]
        ]
    if p.get("visit_date") is None:
        return []
    invoices = []
    if p.get("invoice_amount") is not None:
        invoices.append({"amount": p["invoice_amount"],
                         "description": p["invoice_description"]})
    return [{
        "visit_date": p["visit_date"],
        "procedures": p["procedures"],
        "next_appointment": p["next_appointment"],
        "invoices": invoices,
    }]


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
            ip TEXT,
            reason TEXT
        );
    """)
    for i, p in enumerate(patients):
        conn.execute(
            "INSERT INTO patients (codice_fiscale, patient_name, phone) VALUES (?, ?, ?)",
            (p["cf"], p["name"], p["phone"]),
        )
        for j, visit in enumerate(patient_visits(p)):
            source_path = f"eval/{i}-{j}.json"
            conn.execute(
                "INSERT INTO visits (codice_fiscale, visit_date, procedures, clinical_notes,"
                " next_appointment, source_path) VALUES (?, ?, ?, ?, ?, ?)",
                (p["cf"], visit["visit_date"], json.dumps(visit["procedures"]), "",
                 visit["next_appointment"], source_path),
            )
            visit_id = conn.execute(
                "SELECT id FROM visits WHERE source_path = ?", (source_path,)
            ).fetchone()["id"]
            for k, invoice in enumerate(visit["invoices"]):
                conn.execute(
                    "INSERT INTO invoices (codice_fiscale, visit_id, line_index, amount,"
                    " description) VALUES (?, ?, ?, ?, ?)",
                    (p["cf"], visit_id, k, invoice["amount"], invoice["description"]),
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
    #
    # the thousands-separated forms matter now that the invoices context
    # carries a python-computed total (chat.invoice_context_lines): a total
    # crosses 1000 far more easily than a single line does, and format_amount
    # renders 1234.5 as "€ 1.234,50" in italian and "€1,234.50" in english. a
    # scorer that only knew "1234,50" would score a perfectly faithful answer
    # as a fidelity failure - exactly the mis-scoring this function was
    # written to stop.
    plain_dot = f"{amount:.2f}"
    plain_comma = plain_dot.replace(".", ",")
    grouped_dot = f"{amount:,.2f}"
    grouped_comma = grouped_dot.replace(",", "X").replace(".", ",").replace("X", ".")
    variants = []
    for v in (plain_dot, plain_comma, grouped_dot, grouped_comma):
        for form in (v, f"€{v}", f"€ {v}"):
            if form not in variants:
                variants.append(form)
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

    # 4b. a four-figure total rendered the way format_amount renders it must
    # pass. under 1000 the grouped and plain forms are identical, so this gap
    # only opens once a total is in play.
    result = {"state": "answer", "body": "Il totale è € 1.234,50."}
    case = {"expect_state": "answer", "lang": "it",
            "expect_contains": [{"amount": 1234.5}]}
    ok, detail = score_case(result, case)
    assert ok, f"4b: thousands-separated total should pass, got: {detail}"

    result = {"state": "answer", "body": "Il totale è € 1.230,50."}
    ok, detail = score_case(result, case)
    assert not ok, "4b: a wrong four-figure total must still fail"

    # 5. bare identifiers (tooth number, phone) stay exact-match - a
    # close-but-wrong tooth number must still fail.
    result = {"state": "answer", "body": "hai fatto un intervento sul dente 47."}
    case = {"expect_state": "answer", "lang": "it", "expect_contains": ["46"]}
    ok, detail = score_case(result, case)
    assert not ok, "5: wrong tooth number must still fail"

    # 6. the fixture builder itself. the flat single-visit shape must still
    # produce exactly the rows it produced before the multi-visit change -
    # nine of the eleven original cases depend on it.
    flat = {"cf": "AAAA850010150301", "name": "Anna Verdi", "phone": "3331110001",
            "visit_date": "2026-03-04", "procedures": ["filling 47"],
            "next_appointment": "2026-09-15", "invoice_amount": 120.5,
            "invoice_description": "otturazione"}
    conn = build_db([flat])
    assert conn.execute("SELECT COUNT(*) c FROM visits").fetchone()["c"] == 1, \
        "6: the flat shape must still insert exactly one visit"
    assert conn.execute("SELECT COUNT(*) c FROM invoices").fetchone()["c"] == 1, \
        "6: the flat shape must still insert exactly one invoice"
    empty = {"cf": "CCCC850010150303", "name": "Carla Bianchi", "phone": "3495550003",
             "visit_date": None, "procedures": [], "next_appointment": None,
             "invoice_amount": None, "invoice_description": None}
    assert patient_visits(empty) == [], "6: a patient with no visit_date has no visits"

    # 7. the multi-visit shape. rows land in fixture order, every invoice
    # across every visit is inserted, and get_next_appointment resolves to the
    # LAST visit's value - that last part is the compound condition a
    # single-visit fixture cannot exercise at all, because with one visit
    # every ordering rule gives the same answer.
    multi = {"cf": "DDDD850010150304", "name": "Dario Costa", "phone": "3386660004",
             "visits": [
                 {"visit_date": "2025-11-12", "procedures": ["filling 36"],
                  "next_appointment": "2026-01-20",
                  "invoices": [{"amount": 90.0, "description": "otturazione"}]},
                 {"visit_date": "2026-01-20", "procedures": ["rct 24", "ext 38"],
                  "next_appointment": "2026-04-08",
                  "invoices": [{"amount": 340.0, "description": "cura canalare"},
                               {"amount": 150.0, "description": "estrazione"}]},
                 {"visit_date": "2026-04-08", "procedures": ["seal 16"],
                  "next_appointment": "2026-10-05", "invoices": []},
             ]}
    conn = build_db([multi])
    dates = [r["visit_date"] for r in
             conn.execute("SELECT visit_date FROM visits ORDER BY id").fetchall()]
    assert dates == ["2025-11-12", "2026-01-20", "2026-04-08"], f"7: visit order was {dates}"
    amounts = [r["amount"] for r in
               conn.execute("SELECT amount FROM invoices ORDER BY id").fetchall()]
    assert amounts == [90.0, 340.0, 150.0], f"7: invoice rows were {amounts}"
    visits = patient_accessor.get_visits(multi["cf"], conn)
    assert [v["visit_date"] for v in visits] == dates, "7: accessor lost or reordered a visit"
    assert patient_accessor.get_next_appointment(multi["cf"], conn) == "2026-10-05", \
        "7: next appointment must come from the last visit, not the first"

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
