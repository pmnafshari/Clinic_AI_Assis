import sys

from dental_notes_schema import CF_PATTERN
from storage import init_db, lookup_patient

FIELD_CUES = {
    "phone": "phone",
    "number": "phone",
    "invoice": "invoice",
    "amount": "invoice",
    "appointment": "appointment",
    "date": "date",
    "when": "date",
}


def classify_question(question):
    q = question.lower()
    for cue in FIELD_CUES:
        if cue in q:
            return "exact"
    return "meaning"


def field_for_question(question):
    q = question.lower()
    for cue, field in FIELD_CUES.items():
        if cue in q:
            return field
    return None


def resolve_cf(name, conn):
    rows = conn.execute(
        "SELECT codice_fiscale FROM patients WHERE lower(patient_name) = lower(?)",
        (name,),
    ).fetchall()
    if len(rows) == 0:
        return None
    if len(rows) > 1:
        return [r["codice_fiscale"] for r in rows if CF_PATTERN.match(r["codice_fiscale"])]
    cf = rows[0]["codice_fiscale"]
    if not CF_PATTERN.match(cf):
        return None
    return cf


def extract_name(question):
    tokens = question.split()
    lowered = [t.lower() for t in tokens]
    if "patient" not in lowered:
        return None

    start = lowered.index("patient") + 1
    name_tokens = []
    i = start
    while i < len(tokens) and tokens[i][:1].isupper():
        name_tokens.append(tokens[i])
        i += 1
    if not name_tokens:
        return None

    name = " ".join(name_tokens).strip(" .,!?")
    for suffix in ("'s", "’s"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name.strip(" .,!?")


def answer_exact(cf, field, conn):
    data = lookup_patient(cf, conn)
    if data is None:
        return f"no patient on record with codice fiscale {cf}"

    visits = conn.execute(
        "SELECT source_path, visit_date, next_appointment FROM visits"
        " WHERE codice_fiscale = ? ORDER BY id",
        (cf,),
    ).fetchall()

    if field == "phone":
        value = data["phone"] or "not recorded"
        answer = f"{data['patient_name']}'s phone: {value}"
    elif field == "invoice":
        if not data["invoices"]:
            answer = f"{data['patient_name']} has no invoices on record"
        else:
            total = sum(i["amount"] for i in data["invoices"])
            answer = f"{data['patient_name']}'s invoice total: {total}"
    elif field == "appointment":
        next_appt = visits[-1]["next_appointment"] if visits else None
        value = next_appt or "not recorded"
        answer = f"{data['patient_name']}'s next appointment: {value}"
    elif field == "date":
        last_date = visits[-1]["visit_date"] if visits else None
        value = last_date or "not recorded"
        answer = f"{data['patient_name']}'s last visit date: {value}"
    else:
        answer = f"{data['patient_name']}: unrecognized field {field}"

    if not visits:
        return answer + " (no visit on record to cite)"

    last_visit = visits[-1]
    return answer + f" [source: {last_visit['source_path']}, visit date: {last_visit['visit_date']}]"


def selftest():
    import tempfile
    from datetime import date
    from pathlib import Path

    from dental_notes_schema import DentalNote, Invoice
    from storage import upsert_note_sql

    with tempfile.TemporaryDirectory() as tmp:
        conn = init_db(str(Path(tmp) / "clinic.sqlite"))

        cf = "RSSM800010150100"
        note = DentalNote(
            patient_name="Rossi",
            codice_fiscale=cf,
            phone="333123456",
            visit_date=date(2026, 6, 1),
            invoices=[Invoice(amount=50.0, description="rct")],
            clinical_notes="rct done",
        )
        upsert_note_sql(note, "RSSM800010150100/notes/n1.json", conn)

        cf2 = "VRDL850315150200"
        note2 = DentalNote(
            patient_name="Verdi",
            codice_fiscale=cf2,
            clinical_notes="cleaning done",
        )
        upsert_note_sql(note2, "VRDL850315150200/notes/n1.json", conn)

        # 1. router keys on cue presence alone
        assert classify_question("what is patient rossi's phone number?") == "exact"
        assert classify_question("which patients had a root canal?") == "meaning"
        assert classify_question("what is the phone number on file?") == "exact"

        # 2. name extraction
        assert extract_name("What is patient Rossi's phone number?") == "Rossi"
        assert extract_name("what is patient Mario Rossi's next appointment?") == "Mario Rossi"
        assert extract_name("what is the phone number on file?") is None

        # 3. resolve_cf
        assert resolve_cf("rossi", conn) == cf
        assert resolve_cf("nobody", conn) is None

        # 4. answer_exact behaviors
        answer = answer_exact(cf, "phone", conn)
        assert "333123456" in answer, "phone value missing from answer"
        assert "n1.json" in answer, "citation source_path missing from answer"

        assert "not recorded" in answer_exact(cf2, "phone", conn)
        assert "no invoices" in answer_exact(cf2, "invoice", conn)
        assert "50" in answer_exact(cf, "invoice", conn)
        assert "no patient on record" in answer_exact("ZZZZ000000000000", "phone", conn)

        # 5. full exact-path pipeline, the ROADMAP example question
        question = "What is patient Rossi's phone number?"
        name = extract_name(question)
        resolved_cf = resolve_cf(name, conn)
        full_answer = answer_exact(resolved_cf, "phone", conn)
        assert "333123456" in full_answer, "full pipeline did not return the seeded phone"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    if len(sys.argv) < 2:
        print('usage: python ask.py "<question>"  |  python ask.py --selftest')
        sys.exit(1)

    question = sys.argv[1]
    conn = init_db("db/clinic.sqlite")

    if classify_question(question) == "meaning":
        print("meaning questions land in plan 05-02")
        return

    name = extract_name(question)
    if name is None:
        print("couldn't identify a patient in that question")
        return

    cf = resolve_cf(name, conn)
    if cf is None:
        print(f"no patient named {name} on record")
        return
    if isinstance(cf, list):
        print(f"multiple patients named {name} found, candidates: {', '.join(cf)}")
        typed = input("type the codice fiscale to use: ").strip().upper()
        if not CF_PATTERN.match(typed):
            print("invalid codice fiscale")
            return
        cf = typed

    field = field_for_question(question)
    print(answer_exact(cf, field, conn))


if __name__ == "__main__":
    main()
