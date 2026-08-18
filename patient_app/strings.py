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
    "current_pin_label": {"it": "PIN attuale", "en": "Current PIN"},
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

    # --- chat page chrome ---
    "chat_heading": {"it": "Fai una domanda sui tuoi dati", "en": "Ask about your records"},
    "chat_intro": {
        "it": "Puoi chiedermi delle tue visite, del prossimo appuntamento, delle fatture o "
              "dei tuoi dati anagrafici. Non conservo le tue domande: ogni volta che ricarichi "
              "la pagina riparti da zero.",
        "en": "You can ask me about your visits, your next appointment, your invoices, or "
              "your own details. I don't keep a record of your questions — reloading the page "
              "starts fresh.",
    },
    "chat_examples_heading": {"it": "Puoi chiedere ad esempio:", "en": "You could ask things like:"},
    "chat_example_1": {
        "it": "Quando è il mio prossimo appuntamento?",
        "en": "When is my next appointment?",
    },
    "chat_example_2": {"it": "Che visite ho fatto?", "en": "What visits have I had?"},
    "chat_example_3": {"it": "Quanto devo pagare?", "en": "How much do I owe?"},
    "chat_example_4": {
        "it": "Che numero di telefono avete per me?",
        "en": "What phone number do you have on file for me?",
    },
    "question_label": {"it": "La tua domanda", "en": "Your question"},
    "question_placeholder": {
        "it": "Es. Quando è il mio prossimo appuntamento?",
        "en": "E.g. When is my next appointment?",
    },
    "chat_cta": {"it": "Chiedi", "en": "Ask"},
    "chat_pending_cta": {"it": "Sto cercando...", "en": "Looking it up..."},
    "chat_pending_help": {"it": "Può richiedere qualche secondo.", "en": "This can take a few seconds."},
    "home_cta": {"it": "Fai una domanda", "en": "Ask a question"},

    # --- the four chat response states ---
    "answer_heading": {"it": "Risposta", "en": "Answer"},
    "refusal_heading": {
        "it": "Non ho trovato questa informazione",
        "en": "I couldn't find that in your records",
    },
    # names the four things the chat can answer so a patient learns the
    # surface rather than guessing (D-04)
    "refusal_body": {
        "it": "Non è nei tuoi dati. Posso rispondere a domande su: le tue visite, il prossimo "
              "appuntamento, le fatture e i tuoi dati anagrafici. Prova a chiedere in un altro modo.",
        "en": "That's not in your records. I can answer questions about: your visits, your next "
              "appointment, your invoices, and your own details. Try asking a different way.",
    },
    "deflect_heading": {
        "it": "Questa domanda è per il tuo dentista",
        "en": "That question is for your dentist",
    },
    # reads the same whether the gate caught a real advice request or a false
    # positive - §4.5 tunes the gate toward false positives on purpose, so this
    # copy must never read as an accusation or a malfunction
    "deflect_body": {
        "it": "Non posso dare consigli clinici, nemmeno su dolore o sintomi. Contatta la clinica "
              "per parlarne con il tuo dentista.",
        "en": "I can't give clinical advice, including about pain or symptoms. Contact the clinic "
              "to talk to your dentist about this.",
    },
    "chat_error_heading": {"it": "Non riesco a rispondere ora", "en": "I can't answer right now"},
    "chat_error_body": {
        "it": "Il sistema non è raggiungibile al momento. Riprova tra qualche minuto o contatta "
              "la clinica.",
        "en": "The system isn't reachable right now. Try again in a few minutes or contact the "
              "clinic.",
    },

    # --- chat context labels (Lead A) ---
    # prefixed onto the model's context per route so a bare rendered value
    # (a single date, a name plus a phone number) states what it is instead
    # of relying on the model to infer it - see chat.py step 6.
    "ctx_next_appointment": {"it": "Prossimo appuntamento", "en": "Next appointment"},
    "ctx_invoices": {"it": "Fatture", "en": "Invoices"},
    "ctx_demographics": {"it": "Dati anagrafici", "en": "Personal details"},
    "ctx_visits": {"it": "Visite", "en": "Visits"},
    "ctx_name": {"it": "Nome", "en": "Name"},
    "ctx_phone": {"it": "Telefono", "en": "Phone"},
    "ctx_visit_date": {"it": "Data", "en": "Date"},
    "ctx_procedure": {"it": "Procedura", "en": "Procedure"},
    "ctx_total": {"it": "Totale", "en": "Total"},

    # --- glossary phrase templates, one per dental_shorthand_glossary.json code ---
    # [ASSUMED] italian phrasing not yet native-speaker reviewed, same standing
    # caveat as the rest of this file and the phase 13-02 intent-gate
    # vocabulary - this is the largest single batch of new italian added here
    "proc_rct": {"it": "cura canalare al dente {n}", "en": "root canal treatment on tooth {n}"},
    "proc_ext": {"it": "estrazione del dente {n}", "en": "extraction of tooth {n}"},
    "proc_comp": {
        "it": "otturazione in composito al dente {n}",
        "en": "composite filling on tooth {n}",
    },
    "proc_filling": {"it": "otturazione al dente {n}", "en": "filling on tooth {n}"},
    "proc_perio": {"it": "trattamento parodontale", "en": "periodontal treatment"},
    "proc_opg": {"it": "radiografia panoramica", "en": "panoramic x-ray"},
    "proc_x_ray": {"it": "radiografia", "en": "x-ray"},
    "proc_caries": {"it": "carie individuata al dente {n}", "en": "tooth decay found on tooth {n}"},
    "proc_crown": {"it": "corona al dente {n}", "en": "crown on tooth {n}"},
    "proc_prophy": {"it": "pulizia professionale", "en": "professional cleaning"},
    "proc_scaling": {"it": "ablazione del tartaro", "en": "scaling (tartar removal)"},
    "proc_restoration": {
        "it": "restauro dentale al dente {n}",
        "en": "dental restoration on tooth {n}",
    },
    "proc_seal": {"it": "sigillatura al dente {n}", "en": "fissure sealant on tooth {n}"},
    "proc_abx": {"it": "prescrizione di antibiotico", "en": "antibiotic prescription"},
    "proc_fu": {"it": "controllo di follow-up", "en": "follow-up check"},
    # a code the glossary does not cover must never reach a patient as a raw
    # internal string - this is the fallback every unmapped code renders to
    "proc_unmapped": {
        "it": "un intervento odontoiatrico (chiedi alla clinica per i dettagli)",
        "en": "a dental procedure (ask the clinic for details)",
    },
}


def t(key, lang, **kwargs):
    # unknown key raises: a missing string should break the selftest, not
    # render an empty heading in front of a patient. unknown lang falls back,
    # because a stale cookie must not 500 the login page.
    entry = STRINGS[key]
    text = entry.get(lang) or entry[DEFAULT_LANGUAGE]
    return text.format(**kwargs) if kwargs else text
