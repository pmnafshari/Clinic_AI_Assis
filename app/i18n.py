"""P29 E-001 (owner, 2026-11-04): the staff app in English and Italian.

English is the source text, written in the templates as {{ _("...") }}; the Italian is in strings_it.py under that
same text. The language is the browser's choice (a cookie, as on the portal), and the switch keeps the page. Italian
wording awaits a native speaker's review, like the portal's. staff_i18n_selftest.py fails on any text without
Italian and on any converted page with text left outside _().
"""
import re

from flask import has_request_context, request
from markupsafe import Markup, escape

from .strings_it import IT

LANGUAGES = ("en", "it")
COOKIE = "staff_lang"
DAYS = {"en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
        "it": ("lun", "mar", "mer", "gio", "ven", "sab", "dom")}
MONTHS = {"en": ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
          "it": ("gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic")}


# a message's kind: (colour, icon, its marker for tests and styles - never translated, the word shown)
FLASH_KINDS = {"success": ("success", "check-circle-fill", "done", "Done"),
               "removed": ("secondary", "x-circle-fill", "removed", "Removed"),
               "warning": ("warning", "exclamation-triangle-fill", "check this", "Check this"),
               "danger": ("danger", "x-octagon-fill", "not done", "Not done"),
               "error": ("danger", "x-octagon-fill", "not done", "Not done")}
FLASH_NOTE = ("primary", "info-circle-fill", "note", "Note")
ROLES = {"dentist": "dentist", "assistant": "assistant", "admin": "admin"}


def current():
    if not has_request_context():
        return "en"
    lang = request.cookies.get(COOKIE)
    return lang if lang in LANGUAGES else "en"


def _(text, **values):
    if current() == "it":
        text = IT.get(text, text)
    return text.format(**values) if values else text


_PATTERNS = []


def message(text):
    """a sentence another module wrote, perhaps with values in it (an error, a refusal), in the page's language: the
    catalog's own sentence if it is there, else the catalog sentence it was built from, its values translated too"""
    if current() != "it" or not text:
        return text
    if text in IT:
        return IT[text]
    if not _PATTERNS:
        for en, it in IT.items():
            if "{" in en:
                rx = "^" + re.sub(r"\\\{(\w+)\\\}", lambda m: f"(?P<{m.group(1)}>.+?)", re.escape(en)) + "$"
                try:
                    _PATTERNS.append((re.compile(rx), it))
                except re.error:
                    continue
    for rx, it in _PATTERNS:
        found = rx.match(text)
        if found:
            return it.format(**{k: IT.get(v, v) for k, v in found.groupdict().items()})
    return text


def template_text(text, **values):
    """_() in a template: the words were written as page text, so they go out as page text did - &, < and > made
    safe, an apostrophe left as it is - while every value, which may be a patient's, is escaped in full."""
    words = Markup(_(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return words.format(**values) if values else words


def in_context(context, text):
    """the same English word said two ways in Italian (booked: one appointment, or a count) - the catalog key is
    "context|text"; English is the text itself"""
    if current() == "it":
        return IT.get(f"{context}|{text}", IT.get(text, text))
    return text


def markup(text, **values):
    # the few strings that carry their own <strong> or link: the words are ours, every value is escaped
    return Markup(_(text, **{k: escape(v) for k, v in values.items()}))


MONTHS_LONG = {"en": ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                       "November", "December"),
               "it": ("gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre",
                      "novembre", "dicembre")}


def day_label(d, lang=None):
    """Tue 3 Nov / mar 3 nov"""
    lang = lang or current()
    return f"{DAYS[lang][d.weekday()]} {d.day} {MONTHS[lang][d.month - 1]}"


DAYS_LONG = {"en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
             "it": ("lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica")}


def full_date(d):
    """Tuesday 3 November 2026 / martedì 3 novembre 2026"""
    lang = current()
    return f"{DAYS_LONG[lang][d.weekday()]} {d.day} {MONTHS_LONG[lang][d.month - 1]} {d.year}"


def stamp(local, now):
    """3 Nov, 09:30 this year, 3 Nov 2025, 09:30 before it"""
    if local.year == now.year:
        return f"{short_date(local)}, {local:%H:%M}"
    return f"{short_date(local)} {local.year}, {local:%H:%M}"


def weekday(d):
    return DAYS[current()][d.weekday()]


def short_date(d):
    """3 Nov / 3 nov"""
    return f"{d.day} {MONTHS[current()][d.month - 1]}"


def long_date(d):
    """3 November 2026 / 3 novembre 2026"""
    return f"{d.day} {MONTHS_LONG[current()][d.month - 1]} {d.year}"


def month_year(d):
    """November 2026 / novembre 2026"""
    return f"{MONTHS_LONG[current()][d.month - 1]} {d.year}"


def safe_next(target):
    # back to a page of this app only - never another host
    if not target or not target.startswith("/") or target.startswith("//") or target.startswith("/\\"):
        return "/"
    return target.rstrip("?") or "/"
