import difflib
import json
import sys
import urllib.error
import urllib.request

from dental_notes_schema import CF_PATTERN
from extract_note import OllamaUnreachable
from storage import get_collection, init_db, lookup_patient

OLLAMA_URL = "http://localhost:11434/api/generate"
SYNTHESIS_MODEL = "llama3.2:3b"

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
        # questions usually name the surname only ("patient Rossi"), stored
        # names are full ("mario rossi") - fall back to a whole-word match
        rows = conn.execute(
            "SELECT codice_fiscale FROM patients"
            " WHERE ' ' || lower(patient_name) || ' ' LIKE '% ' || lower(?) || ' %'",
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


def fuzzy_lookup(query, conn, limit=8):
    # typo-tolerant patient search for the records screen - always returns a
    # list of candidates for staff to pick from, never a single auto-picked cf
    query_lower = query.lower().strip()
    if not query_lower:
        return []

    tokens = query_lower.split()
    # coarse SQL prefilter: every token must appear somewhere in the name -
    # cheap at this data volume, narrows the set difflib has to score
    clauses = " AND ".join("lower(patient_name) LIKE ?" for _ in tokens)
    params = ["%" + token + "%" for token in tokens]
    sql = "SELECT codice_fiscale, patient_name FROM patients WHERE " + clauses
    rows = conn.execute(sql, params).fetchall()

    if not rows:
        # prefilter found no substring match - widen to a full fuzzy pass so
        # a misspelled name still gets a chance to score
        rows = conn.execute("SELECT codice_fiscale, patient_name FROM patients").fetchall()

    candidates = []
    for row in rows:
        name = row["patient_name"]
        # lowercase both sides - stored names keep the operator's capitalization
        # and difflib is case-sensitive
        name_lower = name.lower()
        name_tokens = name_lower.split()
        # a row the prefilter already matched contains every token as a
        # substring - difflib must not veto it
        close = all(token in name_lower for token in tokens)
        # otherwise match a short/partial query against individual name tokens
        # ("ros" vs "rossi" scores well; "ros" vs "mario rossi" as a whole
        # would not) - fall back to comparing the full strings for a query
        # that already reads like a full name
        if not close:
            close = any(
                difflib.get_close_matches(token, name_tokens, n=1, cutoff=0.6) for token in tokens
            )
        if not close:
            close = bool(difflib.get_close_matches(query_lower, [name_lower], n=1, cutoff=0.6))
        if close:
            candidates.append((row["codice_fiscale"], name))

    return candidates[:limit]


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


def call_model(prompt, model, urlopen=urllib.request.urlopen):
    payload = {"model": model, "prompt": prompt, "stream": False, "options": {"temperature": 0}}
    data = json.dumps(payload).encode()
    req = urllib.request.Request(OLLAMA_URL, data=data, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=120) as resp:
            body = json.load(resp)
    except urllib.error.URLError:
        raise OllamaUnreachable(f"Ollama not reachable - run: ollama run {model}")
    return body.get("response", "")


def answer_meaning(question, collection, urlopen=urllib.request.urlopen, k=4):
    result = collection.query(query_texts=[question], n_results=k)
    documents = result.get("documents", [[]])[0]
    metadatas = result.get("metadatas", [[]])[0]

    if not documents:
        return "not in records"

    # pair each chunk with its patient so the model can (and must) attribute
    # facts by name - the note text alone never contains the patient's name
    lines = []
    for doc, meta in zip(documents, metadatas):
        lines.append(f"patient {meta['patient_name']}: {doc}")
    context = "\n".join(lines)
    prompt = (
        "Answer ONLY from the context below, naming the relevant patients.\n"
        "If not present, say 'not in records'.\n"
        "Context:\n"
        f"{context}\n"
        f"Question: {question}"
    )
    answer = call_model(prompt, SYNTHESIS_MODEL, urlopen=urlopen)

    # a not-in-records answer cites nothing - there is no record behind it
    if "not in records" in answer.lower():
        return "not in records"

    # cite only the retrieved records the answer actually names; if the answer
    # names nobody specific, keep every retrieved source (conservative)
    answer_lower = answer.lower()
    cited = []
    for meta in metadatas:
        for token in meta["patient_name"].lower().split():
            if len(token) > 2 and token in answer_lower:
                cited.append(meta)
                break
    if not cited:
        cited = metadatas

    citations = [
        f"[source: {meta['source_path']}, visit date: {meta['visit_date']}, patient: {meta['patient_name']}]"
        for meta in cited
    ]
    return answer + " " + " ".join(citations)


def selftest():
    import tempfile
    from datetime import date
    from pathlib import Path

    from dental_notes_schema import DentalNote, Invoice
    from storage import upsert_note_chroma, upsert_note_sql

    with tempfile.TemporaryDirectory() as tmp:
        conn = init_db(str(Path(tmp) / "clinic.sqlite"))

        # full names as stored by the real loader - questions say "patient Rossi",
        # the db says "mario rossi", so these fixtures must not be surname-only
        cf = "RSSM800010150100"
        note = DentalNote(
            patient_name="mario rossi",
            codice_fiscale=cf,
            phone="333123456",
            visit_date=date(2026, 6, 1),
            invoices=[Invoice(amount=50.0, description="rct")],
            clinical_notes="rct done",
        )
        upsert_note_sql(note, "RSSM800010150100/notes/n1.json", conn)

        cf2 = "VRDL850315150200"
        note2 = DentalNote(
            patient_name="luigi verdi",
            codice_fiscale=cf2,
            clinical_notes="cleaning done",
        )
        upsert_note_sql(note2, "VRDL850315150200/notes/n1.json", conn)

        # two patients sharing a surname, for the multi-match prompt path
        cf3 = "BNCP900010150300"
        cf4 = "BNCC910010150400"
        upsert_note_sql(DentalNote(patient_name="paola bianchi", codice_fiscale=cf3),
                        "BNCP900010150300/notes/n1.json", conn)
        upsert_note_sql(DentalNote(patient_name="carlo bianchi", codice_fiscale=cf4),
                        "BNCC910010150400/notes/n1.json", conn)

        # names typed through the web form keep their capitalization - search
        # must find them too
        cf5 = "GLLA920010150500"
        upsert_note_sql(DentalNote(patient_name="Anna Gialli", codice_fiscale=cf5),
                        "GLLA920010150500/notes/n1.json", conn)

        # 1. router keys on cue presence alone
        assert classify_question("what is patient rossi's phone number?") == "exact"
        assert classify_question("which patients had a root canal?") == "meaning"
        assert classify_question("what is the phone number on file?") == "exact"

        # 2. name extraction
        assert extract_name("What is patient Rossi's phone number?") == "Rossi"
        assert extract_name("what is patient Mario Rossi's next appointment?") == "Mario Rossi"
        assert extract_name("what is the phone number on file?") is None

        # 3. resolve_cf: exact full name, surname fallback, no match, multi-match
        assert resolve_cf("mario rossi", conn) == cf
        assert resolve_cf("rossi", conn) == cf, "surname alone must resolve against a full name"
        assert resolve_cf("Rossi", conn) == cf
        assert resolve_cf("nobody", conn) is None
        assert sorted(resolve_cf("bianchi", conn)) == sorted([cf3, cf4]), \
            "shared surname must return the candidate list for the cf prompt"

        # 3b. fuzzy_lookup: typo-tolerant search, always a list, never a write
        assert fuzzy_lookup("", conn) == [], "empty query must short-circuit to no candidates"
        assert fuzzy_lookup("   ", conn) == [], "whitespace-only query must short-circuit too"

        ros_hits = fuzzy_lookup("ros", conn)
        rosi_hits = fuzzy_lookup("rosi", conn)
        reordered_hits = fuzzy_lookup("rossi mario", conn)
        partial_multi_hits = fuzzy_lookup("mario ro", conn)
        bianchi_hits = fuzzy_lookup("bianchi", conn)

        for hits in (ros_hits, rosi_hits, reordered_hits, partial_multi_hits, bianchi_hits):
            assert isinstance(hits, list), "fuzzy_lookup must always return a list"
            for item in hits:
                assert isinstance(item, tuple) and len(item) == 2, \
                    "every candidate must be a (cf, name) tuple"

        assert (cf, "mario rossi") in ros_hits, "partial query 'ros' must surface mario rossi"
        assert (cf, "mario rossi") in rosi_hits, "misspelled 'rosi' must still surface mario rossi"
        assert fuzzy_lookup("zzzzzz", conn) == [], "no-match query must return [], never crash"
        assert (cf, "mario rossi") in reordered_hits, \
            "reordered tokens ('rossi mario') must still find mario rossi"
        assert (cf, "mario rossi") in partial_multi_hits, \
            "multi-token partial query ('mario ro') must still find mario rossi"

        bianchi_cfs = {c for c, n in bianchi_hits}
        assert cf3 in bianchi_cfs and cf4 in bianchi_cfs, \
            "two similarly-named patients must both appear - no false-confident single pick"

        # 3c. a capitalized stored name must be reachable whatever the query casing
        for q in ("gia", "Gia", "GIA", "gialli", "anna gialli", "Anna Gia"):
            hits = fuzzy_lookup(q, conn)
            assert (cf5, "Anna Gialli") in hits, \
                f"query {q!r} must surface the capitalized name 'Anna Gialli'"

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

        # 6. meaning path: chroma retrieval + grounded synthesis, fully offline
        collection = get_collection(str(Path(tmp) / "chroma"))
        chroma_note = DentalNote(
            patient_name="mario rossi",
            codice_fiscale=cf,
            visit_date=date(2026, 6, 1),
            procedures=["root canal"],
            clinical_notes="root canal treatment on tooth 26",
        )
        upsert_note_chroma(chroma_note, "RSSM800010150100/notes/n1.json", collection)
        chroma_note2 = DentalNote(
            patient_name="luigi verdi",
            codice_fiscale=cf2,
            visit_date=date(2026, 6, 3),
            procedures=["cleaning"],
            clinical_notes="routine cleaning, no issues",
        )
        upsert_note_chroma(chroma_note2, "VRDL850315150200/notes/n1.json", collection)

        def fake_urlopen(req, timeout=120):
            class FakeResponse:
                def __enter__(self):
                    return self

                def __exit__(self, *exc_info):
                    return False

                def read(self):
                    return json.dumps({"response": "Rossi had a root canal."}).encode()

            return FakeResponse()

        meaning_answer = answer_meaning(
            "which patients had a root canal?", collection, urlopen=fake_urlopen
        )
        assert "root canal" in meaning_answer, "synthesized answer missing"
        assert "n1.json" in meaning_answer, "citation source_path missing from meaning answer"
        assert "2026-06-01" in meaning_answer, "citation visit_date missing from meaning answer"
        # the answer names rossi only - verdi's retrieved chunk must not be cited
        assert "verdi" not in meaning_answer, "cited a retrieved patient the answer does not name"

        # a not-in-records answer from the model carries no citations
        def fake_urlopen_miss(req, timeout=120):
            class FakeResponse:
                def __enter__(self):
                    return self

                def __exit__(self, *exc_info):
                    return False

                def read(self):
                    return json.dumps({"response": "Not in records."}).encode()

            return FakeResponse()

        miss_answer = answer_meaning(
            "which patients had implants?", collection, urlopen=fake_urlopen_miss
        )
        assert miss_answer == "not in records", "not-in-records answer must carry no citations"

        # empty retrieval -> explicit not-in-records, no model call needed
        empty_collection = get_collection(str(Path(tmp) / "chroma_empty"))
        empty_answer = answer_meaning("anything at all?", empty_collection, urlopen=fake_urlopen)
        assert empty_answer == "not in records"

        # unreachable Ollama gives a clear error naming the synthesis model
        def boom(*a, **k):
            raise urllib.error.URLError("connection refused")

        try:
            call_model("any prompt", SYNTHESIS_MODEL, urlopen=boom)
            raise AssertionError("unreachable Ollama should raise OllamaUnreachable")
        except OllamaUnreachable as e:
            assert SYNTHESIS_MODEL in str(e)

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
        collection = get_collection("db/chroma")
        print(answer_meaning(question, collection))
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
