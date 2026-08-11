"""the request path for a patient's question.

this module runs the no-advice gate before anything else, routes deterministically
with no model anywhere in the routing decision, and calls a model only to phrase
rows that have already been fetched and scoped. §3.5 of the architecture doc puts
the intent gate at step 3 and the accessor call at step 4 - that ordering is a
binding security property (§7.1), not a style choice, and it is enforced here by
plain code structure: each step in answer_question is a separate early return, so
the gate always runs before the accessor and the accessor always runs before the
model.
"""

import ast
import inspect
import json
import re
import sqlite3
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

import patient_accessor
from auth import log_audit

from .render import render_demographics, render_invoices, render_next_appointment, render_visits

OLLAMA_URL = "http://localhost:11434/api/generate"
# llama3.2:3b, not dental-notes - dental-notes is tuned for note-to-JSON
# extraction and is the wrong tool for patient-facing prose, and the 16GB
# rule means only one big model loaded at a time
ANSWER_MODEL = "llama3.2:3b"

ROUTES = ("next_appointment", "invoices", "demographics", "visits")

# --- the D-02 pre-retrieval gate ---
#
# [ASSUMED] this italian vocabulary has had no native-speaker review. §7.1
# marks gate vocabulary and thresholds as explicitly non-binding, and phase 19
# tunes it against the patient_deflect rows this phase starts writing.
#
# checked in the order below, not the order a reader might expect from §4.2's
# table: "what should i take" (treatment) and "should i" (advice) overlap as
# substrings, and checking advice first would deflect "What should I take for
# this?" as advice instead of routing it to the treatment category the UI-SPEC
# acceptance case expects. treatment and symptom vocabulary do not collide
# with each other, so this reorder only resolves the one real conflict.
_ADVICE_CATEGORY_ORDER = ("treatment", "symptom", "advice")

ADVICE_TRIGGERS = {
    # do not add a bare "devo" here - UI-SPEC example chip 3 is "Quanto devo
    # pagare?", a pure invoice question, and a bare "devo" would deflect it.
    # only the multi-word advice phrasings below are safe.
    "advice": (
        r"dovrei", r"cosa devo fare", r"devo fare", r"e normale", r"e' normale",
        r"va bene se", r"should i", r"is this normal", r"do i need",
    ),
    "symptom": (
        r"dolore", r"fa male", r"mi fa male", r"gonfiore", r"gonfio", r"sanguina",
        r"sangue", r"infiammazione", r"ascesso", r"febbre",
        r"pain", r"hurts?", r"swelling", r"swollen", r"bleeding", r"abscess", r"fever",
    ),
    # do not add a bare "cura" here - "cura canalare" is a rendered procedure
    # name, and "quando ho fatto la cura canalare?" is a legitimate records
    # question, not a treatment request.
    "treatment": (
        r"antibiotico", r"antidolorifico", r"medicina", r"farmaco", r"cosa posso prendere",
        r"antibiotic", r"painkiller", r"medicine", r"medication", r"what should i take",
    ),
}


def normalise(text):
    """lowercase, NFKD, combining marks stripped, whitespace collapsed."""
    text = text.lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text).strip()


def advice_category(question):
    """-> 'advice' | 'symptom' | 'treatment' | None. Runs on raw text, normalises
    internally. Pure - reads only the question text, so nothing can be fetched
    from inside it.
    """
    text = normalise(question)
    for category in _ADVICE_CATEGORY_ORDER:
        for pattern in ADVICE_TRIGGERS[category]:
            if re.search(rf"\b{pattern}\b", text):
                return category
    return None


# --- the D-01 deterministic classifier ---
#
# same dict-of-cues shape as ask.FIELD_CUES/field_for_question, extended to
# four destinations and two languages. evaluated in ROUTES order, most
# specific to broadest, so a question matching two tables resolves the same
# way every time.
ROUTE_TRIGGERS = {
    "next_appointment": (
        r"prossimo appuntamento", r"appuntamento", r"prossima visita", r"quando torno",
        r"next appointment", r"appointment", r"when.*come back",
    ),
    "invoices": (
        r"fattura", r"fatture", r"pagare", r"pagamento", r"quanto devo", r"costo", r"importo",
        r"conto", r"invoice", r"bill", r"owe", r"cost", r"amount", r"pay",
    ),
    # anagrafic\w* covers anagrafici/anagrafica/anagrafico - the stem alone
    # would either miss the real word or (with a bare trailing \b) never
    # match it at all, since none of those inflections is the literal
    # string "anagrafic".
    "demographics": (
        r"telefono", r"numero", r"recapito", r"miei dati", r"anagrafic\w*", r"come mi chiamo",
        r"phone", r"number", r"contact", r"my details", r"my name",
    ),
    # intervent\w*/trattament\w* cover intervento/interventi and
    # trattamento/trattamenti the same way anagrafic\w* does above.
    "visits": (
        r"visita", r"visite", r"che visite", r"cosa ho fatto", r"storico", r"intervent\w*",
        r"trattament\w*", r"visit", r"visits", r"procedure", r"treatment", r"what have i had",
    ),
}


def route_question(question):
    """-> one of ROUTES, or None when nothing matches. Pure, deterministic, no
    model - reads only the question text and never reaches the database.
    """
    text = normalise(question)
    for route in ROUTES:
        for pattern in ROUTE_TRIGGERS[route]:
            if re.search(rf"\b{pattern}\b", text):
                return route
    return None


LANGUAGE_NAMES = {"it": "Italian", "en": "English"}

# reviewable in one place, per this plan's own instruction. the "no clinical
# advice" line below is NOT what enforces D-11/binding property 4 - §4.1 and
# §7.1 are explicit that a system-prompt instruction does not satisfy it.
# answer_question's step 2, the advice_category gate, is what does; this line
# is only a second, non-load-bearing layer on top of it.
ANSWER_PROMPT = (
    "Answer only using the context below, in {language}.\n"
    "If the context does not contain the answer, reply with exactly NOT_IN_RECORDS"
    " and nothing else.\n"
    "Give no clinical advice, opinion or recommendation.\n"
    "Context:\n"
    "{context}\n"
    "---\n"
    "Patient question: {question}"
)


def _call_model(prompt, urlopen):
    payload = {
        "model": ANSWER_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0},
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        OLLAMA_URL, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urlopen(req, timeout=120) as resp:
            body = json.load(resp)
    except urllib.error.URLError:
        return None
    return body.get("response", "")


def answer_question(question, cf, conn, lang, ip=None, urlopen=urllib.request.urlopen):
    """-> {"state": "answer"|"refusal"|"deflection"|"error",
           "body": str|None,        # generated prose, only when state == "answer"
           "target": str}           # audit/debug label: route name, ':empty',
                                     # 'unrouted', the deflection category, or
                                     # 'model_unreachable'

    each step below is a separate early return so the ordering is visible in
    one screen of code: the gate (step 2) always runs before the accessor
    (step 4), and the accessor always runs before the model (step 7).
    """
    # 1. blank question - nothing to route, nothing to gate against
    if not question or not question.strip():
        return {"state": "refusal", "body": None, "target": "unrouted"}

    # 2. the D-02 gate. nothing below this point has run yet - no accessor
    # call, no retrieval, no model call. the deflection body is a fixed
    # string the template renders; §4.5 forbids invoking the model to phrase
    # it, so no prompt is ever built for a deflected question.
    category = advice_category(question)
    if category is not None:
        log_audit(conn, cf, "patient", "patient_deflect", category, allowed=0, ip=ip)
        return {"state": "deflection", "body": None, "target": category}

    # 3. D-04: an unroutable question refuses with a capability hint (the
    # hint copy lives in the template, not here) and never reaches an
    # accessor or the model.
    route = route_question(question)
    if route is None:
        return {"state": "refusal", "body": None, "target": "unrouted"}

    # 4. exactly one accessor call for the matched route. cf is a parameter
    # of this function, sourced by the caller from the patient's own session
    # row - it is never derived from the question text. the name-to-cf
    # lookup ask.py uses for staff Q&A is the inverse operation and is not
    # available here.
    if route == "next_appointment":
        data = patient_accessor.get_next_appointment(cf, conn)
    elif route == "invoices":
        data = patient_accessor.get_invoices(cf, conn)
    elif route == "demographics":
        data = patient_accessor.get_demographics(cf, conn)
    else:
        data = patient_accessor.get_visits(cf, conn)

    # 5. an empty result refuses before the model is ever called - this is
    # roadmap SC4's exact case, and a question with no matching record data
    # must not cost a multi-second generation to refuse.
    if not data:
        return {"state": "refusal", "body": None, "target": f"{route}:empty"}

    # 6. render the already-scoped rows through patient_app/render.py, in
    # the patient's own language (D-06)
    if route == "next_appointment":
        lines = [render_next_appointment(data, lang)]
    elif route == "invoices":
        lines = render_invoices(data, lang)
    elif route == "demographics":
        lines = render_demographics(data, lang)
    else:
        lines = render_visits(data, lang)
    context = "\n".join(lines)

    # 7. build the prompt and call the model. there is nothing about another
    # patient anywhere in this prompt to reveal - §6.1's T1 mitigation.
    language_name = LANGUAGE_NAMES.get(lang, LANGUAGE_NAMES["it"])
    prompt = ANSWER_PROMPT.format(language=language_name, context=context, question=question)
    reply = _call_model(prompt, urlopen)
    if reply is None:
        return {"state": "error", "body": None, "target": "model_unreachable"}

    # 8. the sentinel, not a natural-language phrase - an italian answer
    # would not reliably carry the english words "not in records"
    if "NOT_IN_RECORDS" in reply:
        return {"state": "refusal", "body": None, "target": f"{route}:empty"}
    return {"state": "answer", "body": reply.strip(), "target": route}


# audit scope, stated deliberately: this module writes exactly one kind of
# row, the patient_deflect row in step 2 above. it does not write a row per
# served answer - D-05 keeps CHAT-07's every-interaction audit in phase 19,
# and the D-09 scope-mismatch row is already written inside patient_accessor
# itself. the absence of a per-answer row here is a decision, not an
# omission, so phase 19 knows exactly what it is adding.
#
# D-03: each call to answer_question is self-contained. there is no
# parameter and no module-level state carrying anything from one call into
# the next - a follow-up like "and the one before that?" will not resolve,
# and that tradeoff is accepted by decision, not by accident.


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        conn = sqlite3.connect(str(Path(tmp) / "clinic.sqlite"))
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
        conn.commit()

        # 0. fixture: two patients, each with a visit, a next appointment
        # and an invoice, so every route has real data for patient A and a
        # distinct set of values for patient B (section 7's leak check).
        cf_a = "AAAA800010150100"
        cf_b = "BBBB850315150200"
        missing_cf = "ZZZZ000000000000"

        conn.execute(
            "INSERT INTO patients (codice_fiscale, patient_name, phone) VALUES (?, ?, ?)",
            (cf_a, "anna alfa", "111000111"),
        )
        conn.execute(
            "INSERT INTO patients (codice_fiscale, patient_name, phone) VALUES (?, ?, ?)",
            (cf_b, "bruno beta", "222000222"),
        )
        conn.execute(
            "INSERT INTO visits (codice_fiscale, visit_date, procedures, clinical_notes,"
            " next_appointment, source_path) VALUES (?, ?, ?, ?, ?, ?)",
            (cf_a, "2026-06-01", json.dumps(["filling 14"]), "note a", "2026-09-01", "a/n1.json"),
        )
        conn.execute(
            "INSERT INTO visits (codice_fiscale, visit_date, procedures, clinical_notes,"
            " next_appointment, source_path) VALUES (?, ?, ?, ?, ?, ?)",
            (cf_b, "2026-06-02", json.dumps(["cleaning"]), "note b", "2026-09-02", "b/n1.json"),
        )
        visit_id_a = conn.execute(
            "SELECT id FROM visits WHERE source_path = ?", ("a/n1.json",)
        ).fetchone()["id"]
        visit_id_b = conn.execute(
            "SELECT id FROM visits WHERE source_path = ?", ("b/n1.json",)
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO invoices (codice_fiscale, visit_id, line_index, amount, description)"
            " VALUES (?, ?, ?, ?, ?)",
            (cf_a, visit_id_a, 0, 80.0, "filling 14"),
        )
        conn.execute(
            "INSERT INTO invoices (codice_fiscale, visit_id, line_index, amount, description)"
            " VALUES (?, ?, ?, ?, ?)",
            (cf_b, visit_id_b, 0, 40.0, "cleaning"),
        )
        conn.commit()

        def _fake_response(text):
            class FakeResponse:
                def __enter__(self):
                    return self

                def __exit__(self, *exc_info):
                    return False

                def read(self):
                    return json.dumps({"response": text}).encode()

            return FakeResponse()

        def canned_urlopen(req, timeout=120):
            return _fake_response("your next visit is on record")

        def not_in_records_urlopen(req, timeout=120):
            return _fake_response("NOT_IN_RECORDS")

        def raising_urlopen(req, timeout=120):
            raise AssertionError("model reached")

        def unreachable_urlopen(req, timeout=120):
            raise urllib.error.URLError("connection refused")

        class _RaisingAccessor:
            def __getattr__(self, name):
                def _raise(*args, **kwargs):
                    raise AssertionError("accessor reached")
                return _raise

        global patient_accessor

        # 1. ordering - binding property 4. an advice-shaped question is
        # deflected before any accessor call, any retrieval and any model
        # call, in each of the three categories, in both languages.
        advice_questions = {
            "advice": ("Dovrei preoccuparmi?", "Should I be worried?"),
            "symptom": ("Ho dolore al dente", "I have pain in my tooth"),
            "treatment": ("Posso prendere un antibiotico?", "What should I take for this?"),
        }
        real_accessor = patient_accessor
        patient_accessor = _RaisingAccessor()
        try:
            for category, (it_q, en_q) in advice_questions.items():
                for q in (it_q, en_q):
                    result = answer_question(q, cf_a, conn, "it", urlopen=raising_urlopen)
                    assert result["state"] == "deflection", (
                        "1: an advice-shaped question must be deflected before any "
                        f"accessor call, any retrieval and any model call - {q!r} gave "
                        f"{result['state']!r}"
                    )
                    assert result["target"] == category, f"1: {q!r} target was {result['target']}"
        finally:
            patient_accessor = real_accessor

        # 2. deflection audit (D-05) - one row per deflection, allowed=0, and
        # no patient_query row, since the every-question audit is phase 19's.
        deflect_rows = conn.execute(
            "SELECT * FROM audit_log WHERE action = 'patient_deflect'"
        ).fetchall()
        assert len(deflect_rows) == 6, f"2: expected 6 deflection rows, got {len(deflect_rows)}"
        for row in deflect_rows:
            assert row["role"] == "patient", f"2: role was {row['role']}"
            assert row["allowed"] == 0, "2: deflection row should be allowed=0"
        query_count = conn.execute(
            "SELECT COUNT(*) c FROM audit_log WHERE action = 'patient_query'"
        ).fetchone()["c"]
        assert query_count == 0, "2: patient_query rows are phase 19 scope, none should exist yet"

        # 3. unroutable (D-04) - an off-surface question refuses, distinct
        # from a deflection, and neither stub is ever reached.
        patient_accessor = _RaisingAccessor()
        try:
            for q in ("qual e la capitale della Francia", "what's the weather"):
                result = answer_question(q, cf_a, conn, "it", urlopen=raising_urlopen)
                assert result["state"] == "refusal", f"3: {q!r} state was {result['state']}"
                assert result["target"] == "unrouted", f"3: {q!r} target was {result['target']}"
        finally:
            patient_accessor = real_accessor

        # 4. routed but empty (SC4) - a missing patient gives an empty
        # result on every route, refusing without ever reaching the model.
        empty_questions = {
            "next_appointment": "Quando e il mio prossimo appuntamento?",
            "invoices": "Quanto devo pagare?",
            "demographics": "Che numero di telefono avete per me?",
            "visits": "Che visite ho fatto?",
        }
        for route, q in empty_questions.items():
            result = answer_question(q, missing_cf, conn, "it", urlopen=raising_urlopen)
            assert result["state"] == "refusal", f"4: {route} state was {result['state']}"
            assert result["target"] == f"{route}:empty", f"4: {route} target was {result['target']}"

        # 5. served answer, and the NOT_IN_RECORDS sentinel refusing after a
        # real model call.
        result5 = answer_question("Che visite ho fatto?", cf_a, conn, "it", urlopen=canned_urlopen)
        assert result5["state"] == "answer", f"5: state was {result5['state']}"
        assert result5["body"] == "your next visit is on record", f"5: body was {result5['body']!r}"

        result5b = answer_question(
            "Che visite ho fatto?", cf_a, conn, "it", urlopen=not_in_records_urlopen
        )
        assert result5b["state"] == "refusal", f"5b: state was {result5b['state']}"

        # 6. model unreachable gives the error state, not a raised exception.
        result6 = answer_question(
            "Che visite ho fatto?", cf_a, conn, "it", urlopen=unreachable_urlopen
        )
        assert result6["state"] == "error", f"6: state was {result6['state']}"
        assert result6["target"] == "model_unreachable", f"6: target was {result6['target']}"

        # 7. cross-patient (CHAT-04 at this layer) - the prompt built for
        # patient A carries none of patient B's values.
        captured = {}

        def capturing_urlopen(req, timeout=120):
            captured["prompt"] = json.loads(req.data)["prompt"]
            return canned_urlopen(req, timeout=timeout)

        answer_question("Che visite ho fatto?", cf_a, conn, "it", urlopen=capturing_urlopen)
        prompt = captured["prompt"]
        for leak in ("bruno beta", "222000222", "2026-06-02", "cleaning", "40.0", "40,00"):
            assert leak not in prompt, f"7: patient B's {leak!r} leaked into A's prompt"

        # 8. static guards - the import graph, the write-verb scan and the
        # resolve_cf ban, same technique as patient_accessor's own selftest.
        source = inspect.getsource(sys.modules[__name__])
        tree = ast.parse(source)
        lines = source.splitlines()
        docstring = ast.get_docstring(tree)
        excluded_names = {"selftest", "main"}
        excluded_ranges = [
            (node.lineno, node.end_lineno) for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name in excluded_names
        ]

        def in_excluded_range(lineno):
            return any(start <= lineno <= end for start, end in excluded_ranges)

        scannable_lines = []
        for idx, line in enumerate(lines, start=1):
            if line.lstrip()[:1] == "#":
                continue
            if docstring and docstring in line:
                continue
            if in_excluded_range(idx):
                continue
            scannable_lines.append(line)
        scannable = "\n".join(scannable_lines)

        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        forbidden_imports = {"storage", "ask", "app", "web_auth", "web_session", "extract_note"}
        assert not (forbidden_imports & imported), \
            f"8: patient_app/chat.py must not import {forbidden_imports & imported}"
        assert "resolve_cf" not in scannable, \
            "8: resolve_cf is ask.py's inverse operation, patient code must never call it"
        assert re.search(r"(?i)\b(insert|update|delete|drop|alter|replace)\b", scannable) is None, \
            "8: patient_app/chat.py holds no write path either"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python -m patient_app.chat --selftest")


if __name__ == "__main__":
    main()
