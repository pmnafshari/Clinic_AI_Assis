"""turns stored record values into patient-readable text, in the selected
language. kept separate from the D-01 classifier and the D-02 gate so the
glossary rendering can be proven on its own.
"""

import json
import sys
from pathlib import Path
from datetime import datetime

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


def selftest():
    # 1. split_procedure on the shapes the notes model actually stores,
    # including the deferred-gap "devitalization tooth 21" shape
    assert split_procedure("filling 47") == ("filling", "47")
    assert split_procedure("rct 46") == ("rct", "46")
    assert split_procedure("perio") == ("perio", None)
    assert split_procedure("x-ray") == ("x-ray", None)
    assert split_procedure("seal 16") == ("seal", "16")
    assert split_procedure("devitalization tooth 21") == ("devitalization", "21")

    # 2. render_procedure carries the tooth number and the phrase, not the
    # raw code, in either language
    it_filling = render_procedure("filling 47", "it")
    assert "47" in it_filling and "filling" not in it_filling, it_filling
    en_filling = render_procedure("filling 47", "en")
    assert "47" in en_filling and "filling" in en_filling, en_filling

    # 3. an uncoded procedure never reaches the patient as the raw string
    for lang in ("it", "en"):
        assert render_procedure("devitalization tooth 21", lang) == t("proc_unmapped", lang)

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
    codes = json.load(open(GLOSSARY_PATH))
    for code in codes:
        for lang in ("it", "en"):
            rendered = render_procedure(f"{code} 11", lang)
            assert rendered != t("proc_unmapped", lang), f"{code}/{lang} has no phrase template"

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
