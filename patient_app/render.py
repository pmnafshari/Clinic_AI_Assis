"""turns stored record values into patient-readable text, in the selected
language. kept separate from the D-01 classifier and the D-02 gate so the
glossary rendering can be proven on its own.
"""

import json
import sys
from pathlib import Path
from datetime import datetime

import glossary_review

from .strings import STRINGS, t

REPO_ROOT = Path(__file__).resolve().parent.parent
GLOSSARY_PATH = REPO_ROOT / "dental_shorthand_glossary.json"


def split_procedure(raw):
    # the notes model stores a procedure as one string: code, then an
    # optional tooth number, sometimes with a stray "dente"/"tooth" word
    # between them (the known-deferred devitalizzazione case does this).
    # the first purely-numeric token, wherever it lands, is the tooth number.
    tokens = raw.strip().lower().split()
    if not tokens:
        return "", None
    code = tokens[0]
    tooth = None
    for token in tokens[1:]:
        if token.isdigit():
            tooth = token
            break
    return code, tooth


def render_procedure(raw, lang):
    code, tooth = split_procedure(raw)
    # POL-11: a phrase is a clinical reading of the code. until a clinical
    # owner has approved that code's entry, the patient gets the neutral
    # fallback - never a guess at what the shorthand meant
    if not glossary_review.approved(code):
        if tooth is not None:
            return t("proc_pending_tooth", lang, n=tooth)
        return t("proc_unmapped", lang)
    key = "proc_" + code.replace("-", "_")
    if key not in STRINGS:
        return t("proc_unmapped", lang)
    template = STRINGS[key]["en"]
    if "{n}" in template:
        # a code the glossary does not cover, or a {n} template with no
        # tooth number found, must never reach a patient as a raw code
        # fragment or a rendered literal "None" - fall back either way
        if tooth is None:
            return t("proc_unmapped", lang)
        return t(key, lang, n=tooth)
    return t(key, lang)


def format_date(value, lang):
    # both languages render DD/MM/YYYY - this is an italian clinic, so a
    # locale-switched date format would contradict every other convention
    # the patient already knows. lang is accepted and deliberately unused.
    if not value:
        return value
    try:
        parsed = datetime.strptime(value.strip(), "%Y-%m-%d")
    except (ValueError, AttributeError):
        return value.strip()
    return parsed.strftime("%d/%m/%Y")


def format_amount(value, lang):
    if value is None:
        return ""
    if lang == "it":
        # same two-decimal formatting as english, then swap the separators
        # to the italian convention (. thousands, , decimal)
        formatted = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        return f"€ {formatted}"
    return f"€{value:,.2f}"


def render_demographics(row, lang):
    if row is None:
        return []
    return [row["patient_name"], row["phone"]]


def render_visits(rows, lang):
    lines = []
    for row in rows:
        date = format_date(row["visit_date"], lang)
        procedures = [render_procedure(p, lang) for p in row["procedures"]]
        if procedures:
            lines.append(f"{date}: {', '.join(procedures)}")
        else:
            lines.append(date)
    return lines


def render_next_appointment(value, lang):
    if not value:
        return None
    return format_date(value, lang)


def render_invoices(rows, lang):
    lines = []
    for row in rows:
        amount = format_amount(row["amount"], lang)
        description = row["description"] or ""
        lines.append(f"{amount} - {description}" if description else amount)
    return lines


def agent_text(result, lang, conn=None, now=None):
    """The agent's and the handoff's reply, composed in python (P10).

    The action path never calls a model, so this is where its wording comes
    from. Every branch is a string from STRINGS with parsed values formatted
    in - nothing here is generated.
    """
    import clinic_time

    state = result["state"]
    payload = result.get("payload") or {}

    def period_word(value):
        return t(f"agent_period_{value}", lang)

    def when(row):
        instant = clinic_time.read_instant(row["starts_at"])
        local = clinic_time.to_local(instant)
        return local.strftime("%d/%m/%Y"), local.strftime("%H:%M")

    if state == "handoff":
        parts = [t("handoff_body", lang)]
        if conn is not None:
            import handoff as handoff_mod
            open_now, nxt = handoff_mod.next_opening(conn, now)
            if open_now:
                parts.append(t("handoff_open", lang))
            elif nxt is not None:
                parts.append(t("handoff_closed", lang).format(
                    when=nxt.strftime("%d/%m/%Y %H:%M")))
            else:
                parts.append(t("handoff_closed_unknown", lang))
        parts.append(t("handoff_urgent", lang))
        return " ".join(parts)

    if state == "agent_need":
        if result.get("target") == "book:past_day":
            return t("agent_past_day", lang)
        need = result["need"]
        text = t(f"agent_need_{need}", lang)
        if need == "which":
            listed = []
            for index, row in enumerate(result.get("appointments") or [], start=1):
                day, time = when(row)
                listed.append(f"{index}. {day} {time} - {row['dentist']}")
            return text + " " + " ".join(listed)
        return text

    if state == "agent_propose":
        if result["kind"] == "book":
            return t("agent_propose_book", lang).format(
                day=format_date(payload["day"], lang), period=period_word(payload["period"]))
        day, time = when(result["appointment"])
        return t("agent_propose_cancel", lang).format(
            day=day, time=time, dentist=result["appointment"]["dentist"])

    if state == "agent_done":
        if result["kind"] == "book":
            return t("agent_done_book", lang).format(
                day=format_date(payload["day"], lang), period=period_word(payload["period"]))
        day, time = when(result["appointment"])
        return t("agent_done_cancel", lang).format(day=day, time=time)

    if state == "agent_failed":
        return t(f"agent_failed_{result['kind']}", lang)
    if state in ("agent_cancelled", "agent_nothing", "agent_stale"):
        return t(state, lang)
    return None


def selftest():
    # 1. split_procedure on the shapes the notes model actually stores,
    # including the deferred-gap "devitalization tooth 21" shape
    assert split_procedure("filling 47") == ("filling", "47")
    assert split_procedure("rct 46") == ("rct", "46")
    assert split_procedure("perio") == ("perio", None)
    assert split_procedure("x-ray") == ("x-ray", None)
    assert split_procedure("seal 16") == ("seal", "16")
    assert split_procedure("devitalization tooth 21") == ("devitalization", "21")

    # 1b. POL-11: with the register as it is - no clinical owner, nothing
    # approved - no code is turned into a phrase for a patient. the tooth
    # number, written verbatim in the note, is kept; the code's meaning is not
    codes = json.load(open(GLOSSARY_PATH))
    for code in codes:
        for lang in ("it", "en"):
            assert render_procedure(f"{code} 11", lang) == t("proc_pending_tooth", lang, n="11"), \
                f"1b: pending code {code} was interpreted for a patient"
            assert render_procedure(code, lang) == t("proc_unmapped", lang), f"1b: {code}"
    for phrase in ("root canal", "extraction", "antibiotic", "cura canalare", "estrazione"):
        assert phrase not in render_procedure("rct 11", "en") + render_procedure("ext 11", "it") \
            + render_procedure("abx", "en"), f"1b: {phrase!r} reached a patient"

    # the checks below are about the phrases themselves, so they run against a
    # throwaway register that approves every entry. the real one is untouched
    import hashlib
    import tempfile
    saved = glossary_review.REGISTER_PATH
    tmp_dir = tempfile.mkdtemp()
    fake = Path(tmp_dir) / "approved.json"
    fake.write_text(json.dumps({
        "glossary_sha256": hashlib.sha256(Path(GLOSSARY_PATH).read_bytes()).hexdigest(),
        "clinical_owner": "test fixture",
        "entries": {c: {"status": "approved", "approved_by": "fixture", "approved_at": "2026-09-23"}
                    for c in codes}}))
    glossary_review.REGISTER_PATH = fake

    # 2. render_procedure carries the tooth number and the phrase, not the
    # raw code, in either language
    it_filling = render_procedure("filling 47", "it")
    assert "47" in it_filling and "filling" not in it_filling, it_filling
    en_filling = render_procedure("filling 47", "en")
    assert "47" in en_filling and "filling" in en_filling, en_filling

    # 3. an uncoded procedure never reaches the patient as the raw string
    # (POL-11: an unknown code is never approved, so it keeps its verbatim
    # tooth number the same way a pending code does - and still no raw code)
    for lang in ("it", "en"):
        rendered = render_procedure("devitalization tooth 21", lang)
        assert rendered == t("proc_pending_tooth", lang, n="21"), rendered
        assert "devitalization" not in rendered, "3: a raw code reached the patient"

    # 4. no-tooth template renders fine; a {n} template with no tooth found
    # falls back rather than printing the literal "None"
    for lang in ("it", "en"):
        assert render_procedure("perio", lang) == t("proc_perio", lang)
        assert render_procedure("crown", lang) == t("proc_unmapped", lang)
    assert "None" not in render_procedure("crown", "it")
    assert "None" not in render_procedure("crown", "en")

    # 5. glossary coverage: every code in dental_shorthand_glossary.json
    # renders to something other than the unmapped fallback, in both
    # languages - a future glossary addition without a phrase template
    # fails this, rather than silently degrading to the fallback
    for code in codes:
        for lang in ("it", "en"):
            rendered = render_procedure(f"{code} 11", lang)
            assert rendered != t("proc_unmapped", lang), f"{code}/{lang} has no phrase template"

    glossary_review.REGISTER_PATH = saved
    import shutil
    shutil.rmtree(tmp_dir)

    # 6. dates
    assert format_date("2026-08-12", "it") == "12/08/2026"
    assert format_date("2026-08-12", "en") == "12/08/2026"
    format_date(None, "it")
    format_date("da concordare", "en")

    # 7. amounts, always two decimals
    assert format_amount(45.0, "it") == "€ 45,00"
    assert format_amount(45.0, "en") == "€45.00"
    assert format_amount(50, "it") == "€ 50,00"

    # 8. the four render_* functions on the accessor's exact shapes, and no
    # clinical text can leak through them - the accessor already excludes it,
    # this is a cheap guard against a future accessor change smuggling it in
    demo = render_demographics({"patient_name": "anna alfa", "phone": "111000111"}, "it")
    assert demo == ["anna alfa", "111000111"]
    assert render_demographics(None, "it") == []

    visits = render_visits(
        [
            {"visit_date": "2026-06-01", "procedures": ["filling 47"], "next_appointment": None},
            {"visit_date": "2026-06-02", "procedures": [], "next_appointment": None},
        ],
        "it",
    )
    assert len(visits) == 2
    assert "12/08/2026" not in visits[0]

    assert render_next_appointment(None, "it") is None
    assert render_next_appointment("2026-09-01", "it") == "01/09/2026"

    invoices = render_invoices(
        [{"amount": 80.0, "description": "filling 14"}, {"amount": 40.0, "description": None}],
        "en",
    )
    assert len(invoices) == 2

    blob = json.dumps([demo, visits, invoices])
    assert "clinical" not in blob

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python -m patient_app.render --selftest")


if __name__ == "__main__":
    main()
