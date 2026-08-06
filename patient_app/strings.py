"""bilingual copy for the patient surface.

the italian column is the default and is binding - an english-only build is
missing the half most patients read.

[ASSUMED] the italian strings were written without native-speaker review, the
same caveat already carried against the intent-gate vocabulary. review before
any real patient sees them.
"""

from flask import request

LANGUAGES = ("it", "en")
DEFAULT_LANGUAGE = "it"
LANG_COOKIE_NAME = "patient_lang"


def current_language():
    # the only flask dependency in this module - it earns its place by
    # sitting next to the values it reads, so the page chrome and the error
    # banner can't drift apart on the same response (WR-09)
    lang = request.cookies.get(LANG_COOKIE_NAME)
    return lang if lang in LANGUAGES else DEFAULT_LANGUAGE

STRINGS = {
    "login_heading": {
        "it": "Accedi ai tuoi dati",
        "en": "Access your records",
    },
    "login_body": {
        "it": "Inserisci il tuo codice fiscale e il PIN che ti ha dato la clinica.",
        "en": "Enter your codice fiscale and the PIN the clinic gave you.",
    },
    "cf_label": {"it": "Codice fiscale", "en": "Codice fiscale"},
    "pin_label": {"it": "PIN", "en": "PIN"},
    "login_cta": {"it": "Accedi", "en": "Sign in"},
    "change_heading": {
        "it": "Scegli un nuovo PIN",
        "en": "Choose a new PIN",
    },
    "change_body": {
        "it": "Per la tua sicurezza, scegli un nuovo PIN prima di continuare.",
        "en": "For your security, choose a new PIN before continuing.",
    },
    "change_cta": {"it": "Salva il nuovo PIN", "en": "Save new PIN"},
    "confirm_label": {"it": "Conferma il PIN", "en": "Confirm PIN"},
    "logout_cta": {"it": "Esci", "en": "Sign out"},
    # generic on purpose: this surface is internet-reachable and must not
    # confirm whether a codice fiscale belongs to a patient of this clinic
    "err_bad_credentials": {
        "it": "Codice fiscale o PIN non corretti.",
        "en": "Codice fiscale or PIN is not correct.",
    },
    # specific on purpose: the patient already proved they hold a real
    # credential, and telling them to phone the clinic is a success criterion
    "err_expired": {
        "it": "Il tuo PIN è scaduto. Contatta la clinica per riceverne uno nuovo.",
        "en": "Your PIN has expired. Contact the clinic to get a new one.",
    },
    "err_locked": {
        "it": "Troppi tentativi. Riprova più tardi o contatta la clinica.",
        "en": "Too many attempts. Try again later or contact the clinic.",
    },
    "err_pin_short": {
        "it": "Il PIN deve avere almeno {n} caratteri.",
        "en": "The PIN must be at least {n} characters.",
    },
    "err_pin_mismatch": {
        "it": "I due PIN non coincidono.",
        "en": "The two PINs don't match.",
    },
    "err_pin_weak": {
        "it": "Scegli un PIN meno prevedibile: non tutto uguale e non in sequenza.",
        "en": "Choose a less predictable PIN: not all the same character, and not "
              "a run of consecutive digits.",
    },
    "err_pin_same": {
        "it": "Il nuovo PIN deve essere diverso da quello attuale.",
        "en": "The new PIN must be different from your current one.",
    },
    "home_heading": {"it": "Bentornato", "en": "Welcome back"},
    "home_body": {
        "it": "Da qui potrai fare domande sui tuoi dati.",
        "en": "From here you'll be able to ask about your records.",
    },
    # D-02: this line is rendered to everyone, always, error or not - it is
    # what makes it safe for verify_pin to stop distinguishing "wrong pin"
    # from "unknown codice fiscale". making it conditional re-opens that
    # oracle, so it must never move inside an {% if %} block.
    "help_line": {
        "it": "Problemi ad accedere? Contatta la clinica.",
        "en": "Trouble signing in? Contact the clinic.",
    },
}


def t(key, lang, **kwargs):
    # unknown key raises: a missing string should break the selftest, not
    # render an empty heading in front of a patient. unknown lang falls back,
    # because a stale cookie must not 500 the login page.
    entry = STRINGS[key]
    text = entry.get(lang) or entry[DEFAULT_LANGUAGE]
    return text.format(**kwargs) if kwargs else text
