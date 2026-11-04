"""P29 E-001 (owner, 2026-11-04): the staff app in English and Italian.

English is the source text, written in the templates as {{ _("...") }}; the Italian is in strings_it.py under that
same text. The language is the browser's choice (a cookie, as on the portal), and the switch keeps the page. Italian
wording awaits a native speaker's review, like the portal's. staff_i18n_selftest.py fails on any text without
Italian and on any converted page with text left outside _().
"""
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


def template_text(text, **values):
    """_() in a template: the words were written as page text, so they go out as page text did - &, < and > made
    safe, an apostrophe left as it is - while every value, which may be a patient's, is escaped in full."""
    words = Markup(_(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return words.format(**values) if values else words


def markup(text, **values):
    # the few strings that carry their own <strong> or link: the words are ours, every value is escaped
    return Markup(_(text, **{k: escape(v) for k, v in values.items()}))


def day_label(d, lang=None):
    lang = lang or current()
    return f"{DAYS[lang][d.weekday()]} {d.day} {MONTHS[lang][d.month - 1]}"


def safe_next(target):
    # back to a page of this app only - never another host
    if not target or not target.startswith("/") or target.startswith("//") or target.startswith("/\\"):
        return "/"
    return target.rstrip("?") or "/"
