"""the request path for a patient's question.

this module runs the no-advice gate before anything else, routes deterministically
with no model anywhere in the routing decision, and calls a model only to phrase
rows that have already been fetched and scoped. §3.5 of the architecture doc puts
the intent gate at step 3 and the accessor call at step 4 - that ordering is a
binding security property (§7.1), not a style choice, and it is enforced here by
plain code structure: each step in _answer is a separate early return, so the gate
always runs before the accessor and the accessor always runs before the model.
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

from .render import (
    format_amount,
    format_date,
    render_demographics,
    render_invoices,
    render_next_appointment,
    render_procedure,
)
from .strings import t

OLLAMA_URL = "http://localhost:11434/api/generate"
# llama3.2:3b, not dental-notes - dental-notes is tuned for note-to-JSON
# extraction and is the wrong tool for patient-facing prose, and the 16GB
# rule means only one big model loaded at a time
ANSWER_MODEL = "llama3.2:3b"

ROUTES = ("next_appointment", "invoices", "demographics", "visits")

# Lead A (chat-answer-infidelity, 2026-08-17): a field label per route,
# prefixed onto the context handed to the model - see step 6 below.
CONTEXT_LABEL_KEYS = {
    "next_appointment": "ctx_next_appointment",
    "invoices": "ctx_invoices",
    "demographics": "ctx_demographics",
    "visits": "ctx_visits",
}

# --- the D-02 pre-retrieval gate ---
#
# [ASSUMED] this italian vocabulary has had no native-speaker review. §7.1
# marks gate vocabulary and thresholds as explicitly non-binding. a
# model-assisted extension pass ran on 2026-08-19 under phase 19 D-05 and
# added the clinical symptom vocabulary the §4.2 table missed - gum, abscess,
# fracture and infection terms - plus the reflexive and passato-prossimo
# phrasings the \b-anchored patterns could not reach. that pass is the same
# class of source that produced this list, so it does not discharge §4.2:
# human review by a native or fluent speaker, clinical staff qualifying,
# is still outstanding and this is still not a reviewed enforcement control.
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
        r"va bene se", r"cosa mi consiglia", r"mi consiglia di",
        r"should i", r"is this normal", r"do i need", r"what do you recommend",
    ),
    # no "carie" and no "decay" here. proc_caries renders as "carie
    # individuata al dente {n}" / "tooth decay found on tooth {n}", so both
    # words are procedure names a patient reads in an answer and may quote
    # back as a records question. decay-symptom phrasing still deflects
    # through the generic vocabulary below ("ho un buco nel dente che fa
    # male"), and english gets "cavity"/"cavities", which appear in no
    # rendered phrase. same rule for every other proc_* word - otturazione,
    # estrazione, corona, radiografia, parodontale, pulizia, ablazione,
    # tartaro, sigillatura, filling, extraction, crown, x-ray, cleaning,
    # scaling, sealant, root canal - none of them belongs in this tuple.
    "symptom": (
        r"dolore", r"fa male", r"mi fa male", r"gonfiore", r"gonfio", r"sanguina",
        r"sangue", r"infiammazione", r"ascesso", r"febbre",
        r"gengiv\w*", r"sanguinamento", r"mi sanguina", r"mi sanguinano",
        r"pus", r"infezione", r"infett\w*", r"frattura", r"fratturat\w*",
        r"scheggiat\w*", r"si e rotto", r"si e' rotto", r"mi si e rotto",
        r"mi si e' rotto", r"mi sono rotto", r"si e gonfiat\w*",
        r"si e' gonfiat\w*", r"ho male", r"sto male", r"mi ha fatto male",
        r"ho avuto dolore", r"sensibilita", r"brucia", r"pulsa",
        r"pain", r"hurts?", r"swelling", r"swollen", r"bleeding", r"abscess", r"fever",
        r"gum", r"gums", r"cavity", r"cavities", r"broken tooth", r"cracked",
        r"chipped", r"sore", r"toothache", r"throbbing", r"infected",
        r"infection", r"sensitive", r"sensitivity",
    ),
    # do not add a bare "cura" here - "cura canalare" is a rendered procedure
    # name, and "quando ho fatto la cura canalare?" is a legitimate records
    # question, not a treatment request.
    #
    # "antibiotico"/"antibiotic" do collide with proc_abx ("prescrizione di
    # antibiotico"), so a patient quoting that phrase back deflects. this is
    # pre-existing and accepted: §4.5 says deflect when uncertain, and a
    # question about an antibiotic prescription is the one place that posture
    # costs a records answer. it is the single allowed exception, pinned as
    # such in selftest section 10 - the fix for any other phrase that starts
    # deflecting is to remove the trigger, never to widen the exception.
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
# the envelope block goes LAST, after the context and the question. measured,
# not stylistic: with it sitting between "answer only using the context below"
# and the context itself, the model stopped reading the context for invoices
# and answered from its own knowledge instead - "il costo puo variare tra
# € 200 e € 500" against a context stating € 340,00, five times out of five.
# moving the block below the question restored it, five out of five. keep the
# grounding rule adjacent to the thing it governs.
ANSWER_PROMPT = (
    "Answer only using the context below, in {language}.\n"
    "Give no clinical advice, opinion or recommendation.\n"
    "Context:\n"
    "{context}\n"
    "---\n"
    "Reply with one JSON object and nothing else, in one of these two forms:\n"
    '{{"status": "answer", "text": "<the answer, in {language}>"}}\n'
    '{{"status": "not_in_records"}}\n'
    "Use not_in_records if and only if the context does not contain the answer.\n"
    "Copy the values from the context exactly. Do not add anything not in the context.\n"
    "Patient question: {question}"
)


def visit_context_lines(rows, lang):
    # render_visits packs a whole visit onto one line, "04/03/2026:
    # otturazione al dente 47", and the model reliably drops the tooth number
    # out of that compound form. splitting the date and the procedure onto
    # their own labelled lines recovers it. render.py is left alone - its
    # output is used elsewhere and is proven correct - so the same formatting
    # helpers are called directly here.
    lines = []
    for row in rows:
        lines.append(f"{t('ctx_visit_date', lang)}: {format_date(row['visit_date'], lang)}")
        procedures = [render_procedure(p, lang) for p in row["procedures"]]
        procedures = [p for p in procedures if p]
        if procedures:
            lines.append(f"{t('ctx_procedure', lang)}: {', '.join(procedures)}")
    return lines


def invoice_context_lines(rows, lang):
    # python does the arithmetic, the model only restates it. over three
    # invoice lines the model answered with the first line alone and called it
    # the amount owed; over two it summed them itself and got it right. neither
    # is something to rely on for a patient's bill.
    #
    # the total goes in only above one line. repeated under a single invoice it
    # measurably harms - "€ 120,50 - otturazione" then "Totale: € 120,50" and
    # the model stops giving the amount at all, reading the repeat as a
    # relationship between two facts. one line is already its own total.
    #
    # rounded before formatting: these are floats, and 0.1 + 0.2 must not reach
    # format_amount as 0.30000000000000004.
    lines = render_invoices(rows, lang)
    if len(rows) > 1:
        total = round(sum(row["amount"] for row in rows), 2)
        lines.append(f"{t('ctx_total', lang)}: {format_amount(total, lang)}")
    return lines


def parse_reply(reply):
    """-> (state, text) where state is 'answer', 'not_in_records' or 'invalid'.

    the reply is validated as a structure, never searched for a phrase. a
    paraphrase denylist loses against an open set of model outputs - the model
    produced "Non sono in registri." unprompted, and case alone was enough to
    slip past the old literal check. so anything that is not a well-formed
    envelope comes back 'invalid' and the caller refuses. fail closed: an
    unvalidated string is never handed to a patient.
    """
    try:
        envelope = json.loads(reply)
    except (ValueError, TypeError):
        return "invalid", None
    if not isinstance(envelope, dict):
        return "invalid", None
    status = envelope.get("status")
    if status == "not_in_records":
        return "not_in_records", None
    if status != "answer":
        return "invalid", None
    text = envelope.get("text")
    if not isinstance(text, str) or not text.strip():
        return "invalid", None
    return "answer", text.strip()


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


def _answer(question, cf, conn, lang, ip, urlopen):
    """the routing body. writes nothing - answer_question owns the audit row.

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
        lines = invoice_context_lines(data, lang)
    elif route == "demographics":
        lines = render_demographics(data, lang)
    else:
        lines = visit_context_lines(data, lang)

    # each route wants a different context shape. this is measured against
    # eval_chat.py, not guessed - the per-route split and the shapes below
    # are recorded in .planning/debug/chat-answer-infidelity.md.
    #
    # demographics is the one route that wants per-field labels and NO
    # heading: render_demographics returns [name, phone], two bare values a
    # "Dati anagrafici" heading does not describe, and with the heading the
    # model answers NOT_IN_RECORDS to "what is my phone number" even when the
    # line above reads "Telefono: 3331110001". visits wants the opposite -
    # heading AND per-field labels - and drops the tooth number without them.
    if route == "demographics":
        keys = ("ctx_name", "ctx_phone")
        context = "\n".join(
            f"{t(k, lang)}: {v}" for k, v in zip(keys, lines) if v
        )
    else:
        label = t(CONTEXT_LABEL_KEYS[route], lang)
        context = label + ":\n" + "\n".join(lines)

    # 7. build the prompt and call the model. there is nothing about another
    # patient anywhere in this prompt to reveal - §6.1's T1 mitigation.
    language_name = LANGUAGE_NAMES.get(lang, LANGUAGE_NAMES["it"])
    prompt = ANSWER_PROMPT.format(language=language_name, context=context, question=question)
    reply = _call_model(prompt, urlopen)
    if reply is None:
        return {"state": "error", "body": None, "target": "model_unreachable"}

    # 8. validate the envelope. the old check was an unanchored substring
    # search for NOT_IN_RECORDS, so a paraphrased or re-cased denial fell
    # through and reached the patient verbatim, and a real answer that
    # happened to contain the token was forced to a refusal (DEF-4). both
    # directions go away once the shape is validated instead of the prose.
    #
    # the two refusal reasons stay distinct in the target: the model saying
    # "no data" is a different event from the model drifting off the envelope,
    # and only the second one means something is wrong.
    state, text = parse_reply(reply)
    if state == "not_in_records":
        return {"state": "refusal", "body": None, "target": f"{route}:empty"}
    if state == "invalid":
        return {"state": "refusal", "body": None, "target": f"{route}:invalid_reply"}
    return {"state": "answer", "body": text, "target": route}


def answer_question(question, cf, conn, lang, ip=None, urlopen=urllib.request.urlopen):
    """-> {"state": "answer"|"refusal"|"deflection"|"error",
           "body": str|None,        # generated prose, only when state == "answer"
           "target": str}           # audit/debug label: route name, ':empty',
                                     # 'unrouted', the deflection category, or
                                     # 'model_unreachable'

    _answer does the routing, this writes the row. one call site, reached by
    every return path there is.
    """
    result = _answer(question, cf, conn, lang, ip, urlopen)
    if result["state"] == "deflection":
        action = "patient_deflect"
        allowed = 0
    else:
        action = "patient_query"
        allowed = 1 if result["state"] == "answer" else 0
    log_audit(conn, cf, "patient", action, result["target"], allowed=allowed, ip=ip)
    return result


# audit scope: one row per call, written by the answer_question wrapper. a
# deflection writes patient_deflect with allowed=0, every other state writes
# patient_query, and allowed=1 only for a served answer - a refusal and an
# error are denials. the wrapper exists so a new terminal return added to
# _answer cannot escape its row (§5.5's log-before-respond rule); with a
# log_audit at each early return, the ninth one silently would.
#
# target carries the route label and never the question text - an audit row
# holding what the patient typed would rebuild the transcript phase 17 D-03
# forbids, one row at a time. the D-09 scope-mismatch row is still written
# inside patient_accessor itself.
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
            return _fake_response(
                '{"status": "answer", "text": "your next visit is on record"}'
            )

        def not_in_records_urlopen(req, timeout=120):
            return _fake_response('{"status": "not_in_records"}')

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

        # 2. the audit row (CHAT-07) - every call leaves exactly one row, and
        # the four states are each checked, since the wrapper is only worth
        # having if the state it never sees is the one that still logs.
        deflect_rows = conn.execute(
            "SELECT * FROM audit_log WHERE action = 'patient_deflect'"
        ).fetchall()
        assert len(deflect_rows) == 6, f"2: expected 6 deflection rows, got {len(deflect_rows)}"
        for row in deflect_rows:
            assert row["role"] == "patient", f"2: role was {row['role']}"
            assert row["allowed"] == 0, "2: deflection row should be allowed=0"

        audit_questions = {
            "unrouted": ("qual e la capitale del Giappone", cf_a, raising_urlopen, 0),
            "visits:empty": ("Che visite ho fatto?", missing_cf, raising_urlopen, 0),
            "visits": ("Che visite ho fatto?", cf_a, canned_urlopen, 1),
            "model_unreachable": ("Quali interventi ho avuto?", cf_a, unreachable_urlopen, 0),
        }
        for expected_target, (q, who, stub, expected_allowed) in audit_questions.items():
            result = answer_question(q, who, conn, "it", urlopen=stub)
            assert result["target"] == expected_target, \
                f"2: {q!r} target was {result['target']}, expected {expected_target}"
            row = conn.execute(
                "SELECT * FROM audit_log WHERE action = 'patient_query'"
                " ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            assert row["target"] == expected_target, f"2: row target was {row['target']}"
            assert row["role"] == "patient", f"2: role was {row['role']}"
            assert row["allowed"] == expected_allowed, \
                f"2: {expected_target} row allowed was {row['allowed']}"

        query_count = conn.execute(
            "SELECT COUNT(*) c FROM audit_log WHERE action = 'patient_query'"
        ).fetchone()["c"]
        assert query_count == 4, f"2: expected 4 patient_query rows, got {query_count}"
        deflect_count = conn.execute(
            "SELECT COUNT(*) c FROM audit_log WHERE action = 'patient_deflect'"
        ).fetchone()["c"]
        assert deflect_count == 6, f"2: deflection count moved to {deflect_count}"

        # D-04 - no row carries what the patient typed. asserted, not assumed:
        # a target that quietly started carrying the question would rebuild the
        # transcript phase 17 D-03 forbids.
        targets = [r["target"] for r in conn.execute("SELECT target FROM audit_log").fetchall()]
        for q, _, _, _ in audit_questions.values():
            for target in targets:
                assert q not in (target or ""), f"2: audit target carries question text: {target!r}"

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

        # 9. DEF-4 - the response envelope, and it fails closed. the reply is
        # validated as a structure rather than searched for a phrase: a
        # denylist of paraphrases is a losing game against an open set of
        # model outputs. anything that is not a well-formed answer envelope
        # is a refusal, including the paraphrased and re-cased denials that
        # used to be served to the patient as normal answers.
        def replying(text):
            def _urlopen(req, timeout=120):
                return _fake_response(text)
            return _urlopen

        must_refuse = [
            '{"status": "not_in_records"}',      # the declared no-data reply
            "Non sono in registri.",             # llama3.2:3b said this unprompted
            "That is not in your records.",
            "not_in_records",                    # case alone used to break it
            "NOT_IN_RECORDS",                    # the old bare sentinel
            "",                                  # empty reply
            "   ",
            "I could not find anything.",        # prose, no envelope
            '{"status": "answer"}',              # no text
            '{"status": "answer", "text": ""}',  # empty text
            '{"status": "answer", "text": "   "}',
            '{"status": "answer", "text": 42}',  # text not a string
            '{"status": "elsewhere", "text": "x"}',
            '["not", "an", "object"]',
            '{"status": "answer", "text": "x"',  # truncated json
        ]
        for reply in must_refuse:
            result = answer_question(
                "Che visite ho fatto?", cf_a, conn, "it", urlopen=replying(reply)
            )
            assert result["state"] == "refusal", (
                f"9: {reply!r} must fail closed to a refusal, got {result['state']!r} "
                f"with body {result['body']!r}"
            )
            assert result["body"] is None, f"9: a refusal must carry no body, got {result['body']!r}"

        # a valid envelope is served, and only its text reaches the patient -
        # never the envelope itself
        result9 = answer_question(
            "Che visite ho fatto?", cf_a, conn, "it",
            urlopen=replying('{"status": "answer", "text": "Hai fatto una visita il 01/06/2026."}'),
        )
        assert result9["state"] == "answer", f"9: state was {result9['state']}"
        assert result9["body"] == "Hai fatto una visita il 01/06/2026.", \
            f"9: body was {result9['body']!r}"
        assert "status" not in result9["body"], "9: the envelope must not reach the patient"

        # the inverse consequence in the DEF-4 write-up: the old unanchored
        # substring test forced a genuine answer to a refusal if it happened to
        # contain the token anywhere. a validated envelope does not care what
        # the text says.
        result9b = answer_question(
            "Che visite ho fatto?", cf_a, conn, "it",
            urlopen=replying(
                '{"status": "answer", "text": "La sigla NOT_IN_RECORDS non compare qui."}'
            ),
        )
        assert result9b["state"] == "answer", (
            "9: an answer containing the old sentinel token must still be served - "
            f"got {result9b['state']!r}"
        )

        # the two refusal reasons stay distinguishable in the audit/debug
        # label: the model saying "no data" is not the same event as the model
        # emitting something unparseable, and a spike in the latter means it
        # has drifted off the envelope.
        said_no = answer_question(
            "Che visite ho fatto?", cf_a, conn, "it",
            urlopen=replying('{"status": "not_in_records"}'),
        )
        assert said_no["target"] == "visits:empty", f"9: target was {said_no['target']}"
        malformed = answer_question(
            "Che visite ho fatto?", cf_a, conn, "it", urlopen=replying("Non sono in registri.")
        )
        assert malformed["target"] == "visits:invalid_reply", \
            f"9: target was {malformed['target']}"

        # 10. the gate, both directions (D-06). §7.1 makes the gate binding
        # but leaves its vocabulary explicitly non-binding, so a later edit can
        # narrow it until an advice question walks through, or widen it until
        # the four routes stop answering. both failure modes are pinned here.
        from .strings import STRINGS

        # direction one: every trigger added by the 2026-08-19 pass, in
        # natural patient phrasing rather than the bare keyword, asserted on
        # the category and not merely on non-None - a term landing in the
        # wrong bucket is a real defect, not a near miss.
        gate_deflects = {
            "gengiv\\w*": ("Mi sanguinano le gengive quando lavo i denti", "symptom"),
            "sanguinamento": ("Ho un sanguinamento che non si ferma", "symptom"),
            "mi sanguina": ("Mi sanguina la bocca da stamattina", "symptom"),
            "mi sanguinano": ("Mi sanguinano le gengive da giorni", "symptom"),
            "pus": ("Vedo del pus vicino al dente", "symptom"),
            "infezione": ("Ho un'infezione alla gengiva", "symptom"),
            "infett\\w*": ("Il dente sembra infettato", "symptom"),
            "frattura": ("Ho una frattura al dente davanti", "symptom"),
            "fratturat\\w*": ("Il dente si e fratturato mangiando", "symptom"),
            "scheggiat\\w*": ("Mi sono scheggiato un dente", "symptom"),
            "si e rotto": ("Il dente si e rotto ieri sera", "symptom"),
            "si e' rotto": ("Il dente si e' rotto ieri sera", "symptom"),
            "mi si e rotto": ("Mi si e rotto un dente", "symptom"),
            "mi si e' rotto": ("Mi si e' rotto un dente", "symptom"),
            "mi sono rotto": ("Mi sono rotto un dente masticando", "symptom"),
            "si e gonfiat\\w*": ("La guancia si e gonfiata stanotte", "symptom"),
            "si e' gonfiat\\w*": ("La guancia si e' gonfiata stanotte", "symptom"),
            "ho male": ("Ho male da due giorni", "symptom"),
            "sto male": ("Sto male da quando sono tornato", "symptom"),
            "mi ha fatto male": ("Mi ha fatto male tutta la notte", "symptom"),
            "ho avuto dolore": ("Ho avuto dolore per una settimana", "symptom"),
            "sensibilita": ("Ho sensibilita al freddo", "symptom"),
            "brucia": ("Mi brucia la gengiva", "symptom"),
            "pulsa": ("Mi pulsa il dente da ieri", "symptom"),
            "gum": ("My gum is swollen near the back", "symptom"),
            "gums": ("My gums bleed when I brush", "symptom"),
            "cavity": ("I have a cavity that hurts", "symptom"),
            "cavities": ("Do I have cavities that need looking at", "symptom"),
            "broken tooth": ("I have a broken tooth", "symptom"),
            "cracked": ("I cracked a tooth last night", "symptom"),
            "chipped": ("I chipped a tooth last night", "symptom"),
            "sore": ("My mouth is sore this morning", "symptom"),
            "toothache": ("I have a toothache", "symptom"),
            "throbbing": ("It's throbbing since yesterday", "symptom"),
            "infected": ("I think it's infected", "symptom"),
            "infection": ("I might have an infection", "symptom"),
            "sensitive": ("My tooth is sensitive to cold", "symptom"),
            "sensitivity": ("I get sensitivity when I drink water", "symptom"),
            "cosa mi consiglia": ("Cosa mi consiglia per questo problema?", "advice"),
            "mi consiglia di": ("Mi consiglia di togliere il dente?", "advice"),
            "what do you recommend": ("What do you recommend for this?", "advice"),
        }
        for trigger, (q, expected) in gate_deflects.items():
            got = advice_category(q)
            assert got == expected, \
                f"10: {trigger!r} - {q!r} gave {got!r}, expected {expected!r}"

        # the decay pair. the italian one carries no decay-specific trigger by
        # design - "carie" is proc_caries's rendered phrase and cannot be a
        # trigger - so it has to deflect through the generic symptom
        # vocabulary instead. asserting it is what makes that trade-off a
        # verified behaviour rather than an argument.
        assert advice_category("Ho un buco nel dente che fa male") == "symptom", \
            "10: italian decay-symptom phrasing must deflect through the generic vocabulary"
        assert advice_category("I have a cavity that hurts") == "symptom", \
            "10: english decay-symptom phrasing must deflect through cavity/cavities"

        # and the additions reach the real gate and its audit row, not just
        # the pure function - accessor and model both raise if either is hit.
        real_accessor = patient_accessor
        patient_accessor = _RaisingAccessor()
        try:
            for q in ("Mi sanguinano le gengive quando lavo i denti", "I have a toothache"):
                before = conn.execute(
                    "SELECT COUNT(*) c FROM audit_log WHERE action = 'patient_deflect'"
                ).fetchone()["c"]
                result = answer_question(q, cf_a, conn, "it", urlopen=raising_urlopen)
                assert result["state"] == "deflection", \
                    f"10: {q!r} state was {result['state']}"
                row = conn.execute(
                    "SELECT * FROM audit_log WHERE action = 'patient_deflect'"
                    " ORDER BY rowid DESC LIMIT 1"
                ).fetchone()
                after = conn.execute(
                    "SELECT COUNT(*) c FROM audit_log WHERE action = 'patient_deflect'"
                ).fetchone()["c"]
                assert after == before + 1, f"10: {q!r} wrote {after - before} deflect rows"
                assert row["allowed"] == 0, "10: a deflection row must be allowed=0"
        finally:
            patient_accessor = real_accessor

        # direction two, sweep 1: the four UI chips. chip 3 is "Quanto devo
        # pagare?", which is why no bare "devo" may ever enter the advice
        # tuple - it would deflect a pure invoice question.
        for i in (1, 2, 3, 4):
            for lang in ("it", "en"):
                chip = t(f"chat_example_{i}", lang)
                assert advice_category(chip) is None, \
                    f"10: example chip {i}/{lang} must not deflect - {chip!r} (the bare-devo trap)"

        # sweep 2: every rendered procedure phrase a patient can read off an
        # answer and quote back. discovered by prefix, not listed here, so a
        # procedure added later falls under this check by itself. proc_abx is
        # the single accepted exception - if any other phrase starts
        # deflecting, remove the responsible trigger, never widen this set.
        proc_deflectors = set()
        for key in STRINGS:
            if not key.startswith("proc_"):
                continue
            for lang in ("it", "en"):
                if advice_category(t(key, lang, n=47)) is not None:
                    proc_deflectors.add(key)
        assert proc_deflectors == {"proc_abx"}, \
            f"10: rendered procedure phrases that deflect must be exactly {{'proc_abx'}}, got {proc_deflectors}"
        for lang in ("it", "en"):
            assert advice_category(t("proc_abx", lang)) == "treatment", \
                f"10: proc_abx/{lang} is the recorded exception and must still deflect as treatment"

        # sweep 3: the exact strings the four routes depend on.
        for route, q in empty_questions.items():
            assert advice_category(q) is None, \
                f"10: the {route} route question must not deflect - {q!r}"

        # the marker itself. removing it would claim the §4.2 human review
        # happened; it has not.
        assert "[ASSUMED]" in inspect.getsource(sys.modules[__name__]), \
            "10: the [ASSUMED] marker records that §4.2's human review is still outstanding"

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
