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
    # [ASSUMED] italian below written without native-speaker review, same
    # caveat as the rest of this module
    # --- portal shell (UX-13/UX-14) -----------------------------------
    "nav_overview": {"it": "Riepilogo", "en": "Overview"},
    "nav_chat": {"it": "Assistente", "en": "Assistant"},
    "nav_profile": {"it": "Profilo", "en": "Profile"},
    "nav_billing": {"it": "Pagamenti", "en": "Payments"},

    # phase 45 - the reference's header and overview. the brand names the
    # surface, never the clinic: this app has no clinic name to show.
    "brand_name": {"it": "Portale paziente", "en": "Patient portal"},
    "overview_eyebrow": {"it": "Il tuo riepilogo", "en": "Your overview"},
    "kpi_next_appt": {"it": "Prossimo appuntamento", "en": "Next appointment"},
    "kpi_next_none": {"it": "Nessuno in programma", "en": "Nothing booked"},
    "kpi_next_none_pill": {"it": "chiedine uno", "en": "ask for one"},
    "kpi_requests": {"it": "Richieste in attesa", "en": "Requests waiting"},
    "kpi_requests_wait": {"it": "da confermare", "en": "to confirm"},
    "kpi_requests_clear": {"it": "nessuna", "en": "none"},
    "overview_heading": {"it": "Ciao, {name}", "en": "Hello, {name}"},
    "overview_body": {
        "it": "Da qui puoi chiedere all'assistente delle tue visite, del prossimo appuntamento e delle tue fatture.",
        "en": "From here you can ask the assistant about your visits, your next appointment and your invoices.",
    },
    "overview_chat_title": {"it": "Chiedi all'assistente", "en": "Ask the assistant"},
    "overview_chat_body": {
        "it": "Visite, appuntamenti, fatture e dati anagrafici: l'assistente risponde solo sui tuoi dati.",
        "en": "Visits, appointments, invoices and your details - the assistant answers only about your own records.",
    },

    # UX-14: these say what is missing, and why, rather than showing an
    # empty table that looks broken
    "not_connected": {"it": "Non disponibile", "en": "Not available"},
    # visits and invoices are NOT missing - they are reachable, through the
    # assistant. badging them "not available" was inaccurate, and inaccuracy
    # about what the clinic holds is the one thing this surface must not do.
    "via_assistant": {"it": "Tramite l'assistente", "en": "Via the assistant"},
    "overview_appt_title": {"it": "Prenotazioni", "en": "Appointments"},
    # Phase 42 connected this. the old copy said appointments were "not
    # connected to this portal yet" and the card carried the not_connected
    # badge - both are now false, and leaving either would be the inaccuracy
    # UX-14 exists to prevent.
    "overview_appt_body": {
        "it": "Chiedi un appuntamento e la clinica ti conferma giorno e ora.",
        "en": "Ask for an appointment and the clinic confirms the day and time.",
    },

    # --- appointments (Phase 42, PAPT-01..05) --------------------------
    "nav_appointments": {"it": "Appuntamenti", "en": "Appointments"},
    "appt_heading": {"it": "I tuoi appuntamenti", "en": "Your appointments"},
    "appt_intro": {
        "it": "Qui trovi gli appuntamenti confermati e le richieste in attesa.",
        "en": "Here are your confirmed appointments and any pending requests.",
    },
    "appt_upcoming": {"it": "In programma", "en": "Upcoming"},
    "appt_none": {
        "it": "Non hai appuntamenti in programma.",
        "en": "You have no upcoming appointments.",
    },
    "appt_pending": {"it": "Richieste in attesa", "en": "Pending requests"},
    "appt_pending_badge": {"it": "In attesa", "en": "Awaiting confirmation"},
    # the whole point of the period model: a request has no time, so the
    # copy must never imply one
    "appt_pending_line": {
        "it": "Hai chiesto: {day}, {period}. La clinica ti conferma l'orario.",
        "en": "You asked for: {day}, {period}. The clinic will confirm the time.",
    },
    "appt_morning": {"it": "mattina", "en": "morning"},
    "appt_afternoon": {"it": "pomeriggio", "en": "afternoon"},
    "appt_with": {"it": "con {dentist}", "en": "with {dentist}"},
    "appt_cancel": {"it": "Disdici", "en": "Cancel"},
    "appt_cancel_confirm": {
        "it": "Vuoi disdire questo appuntamento?",
        "en": "Cancel this appointment?",
    },
    "appt_cancelled_ok": {"it": "Appuntamento disdetto.", "en": "Appointment cancelled."},
    "appt_request_title": {"it": "Chiedi un appuntamento", "en": "Request an appointment"},
    "appt_request_day": {"it": "Giorno preferito", "en": "Preferred day"},
    "appt_request_period": {"it": "Quando", "en": "When"},
    "appt_request_reason": {"it": "Motivo (facoltativo)", "en": "Reason (optional)"},
    "appt_request_send": {"it": "Invia richiesta", "en": "Send request"},
    "appt_request_ok": {
        "it": "Richiesta inviata. La clinica ti ricontatta per confermare.",
        "en": "Request sent. The clinic will get back to you to confirm.",
    },
    "appt_request_note": {
        "it": "Non scegli tu l'orario: la clinica lo fissa in base alle disponibilita reali.",
        "en": "You do not pick the time - the clinic sets it from real availability.",
    },
    "appt_error_date": {"it": "Scegli una data valida, da oggi in poi.",
                        "en": "Choose a valid date, today or later."},
    "appt_error_period": {"it": "Scegli mattina o pomeriggio.",
                          "en": "Choose morning or afternoon."},
    "appt_error_generic": {"it": "Non e stato possibile completare la richiesta.",
                           "en": "That could not be completed."},
    "overview_records_title": {"it": "Visite e fatture", "en": "Visits and invoices"},
    "overview_records_body": {
        "it": "I tuoi dati clinici si consultano tramite l'assistente, non come elenco su questa pagina.",
        "en": "Your clinical records are read through the assistant, not as a list on this page.",
    },

    "profile_heading": {"it": "Il tuo profilo", "en": "Your profile"},
    "profile_name": {"it": "Nome", "en": "Name"},
    "profile_phone": {"it": "Telefono", "en": "Phone"},
    "profile_cf": {"it": "Codice fiscale", "en": "Codice fiscale"},
    "profile_missing": {"it": "Non registrato", "en": "Not on file"},
    "profile_note": {
        "it": "Per correggere questi dati contatta la clinica: non si modificano da qui.",
        "en": "To correct these details, contact the clinic - they cannot be changed here.",
    },
    "profile_change_pin": {"it": "Cambia il PIN", "en": "Change your PIN"},

    # consent (P06). the wording itself lives in consent_texts.json
    "consent_heading": {"it": "I tuoi consensi", "en": "Your consents"},
    "consent_template_note": {
        "it": "Testi dimostrativi: questa è una clinica di prova, non un testo legale reale.",
        "en": "Demo wording: this is a demo clinic, not a real legal text.",
    },
    "consent_ai_assistant": {"it": "Assistente del portale", "en": "Portal assistant"},
    "consent_messaging": {"it": "Messaggi e promemoria", "en": "Messages and reminders"},
    "consent_recording": {"it": "Registrazione delle chiamate", "en": "Call recording"},
    "consent_given": {"it": "Dato", "en": "Given"},
    "consent_withdrawn": {"it": "Ritirato", "en": "Withdrawn"},
    "consent_not_asked": {"it": "Non ancora chiesto", "en": "Not asked yet"},
    "consent_outdated": {"it": "Il testo è cambiato: conferma di nuovo", "en": "The wording changed: please confirm again"},
    "consent_give": {"it": "Acconsento", "en": "I agree"},
    "consent_withdraw": {"it": "Ritira il consenso", "en": "Withdraw"},
    "chat_consent_needed": {
        "it": "Per usare l'assistente serve il tuo consenso. Puoi darlo o ritirarlo dal tuo profilo.",
        "en": "The assistant needs your consent first. You can give or withdraw it from your profile.",
    },
    "chat_consent_link": {"it": "Vai al profilo", "en": "Go to your profile"},

    # billing (P07). figures come from the ledger, the same as the staff page
    "bill_heading": {"it": "Fatture e pagamenti", "en": "Invoices and payments"},
    "bill_notice": {
        "it": "I pagamenti sono registrati dalla clinica. Da questo portale non si paga e non passa denaro.",
        "en": "Payments are recorded by the clinic. You cannot pay through this portal and no money passes through it.",
    },
    "bill_outstanding": {"it": "Da pagare", "en": "Still to pay"},
    "bill_unknown_count": {
        "it": "{n} fattura/e in verifica: la clinica non ha ancora registrato se sono state pagate, quindi non sono conteggiate.",
        "en": "{n} invoice(s) being checked: the clinic has not yet recorded whether they were paid, so they are not counted.",
    },
    "bill_visit": {"it": "Visita del {date}", "en": "Visit of {date}"},
    "bill_total": {"it": "Totale", "en": "Total"},
    "bill_paid": {"it": "Pagato", "en": "Paid"},
    "bill_due": {"it": "Scadenza", "en": "Due"},
    "bill_installments": {"it": "Rate", "en": "Installments"},
    "bill_covered": {"it": "saldata", "en": "covered"},
    "bill_open": {"it": "da pagare", "en": "open"},
    "bill_none": {"it": "Nessuna fattura registrata.", "en": "No invoices on record."},
    "bill_state_unknown": {"it": "In verifica", "en": "Being checked"},
    "bill_state_draft": {"it": "In preparazione", "en": "Being prepared"},
    "bill_state_issued": {"it": "Da pagare", "en": "To pay"},
    "bill_state_partially_paid": {"it": "Pagata in parte", "en": "Partly paid"},
    "bill_state_paid": {"it": "Pagata", "en": "Paid"},
    "bill_state_void": {"it": "Annullata", "en": "Cancelled"},

    # data rights (P06)
    "rights_heading": {"it": "I tuoi dati", "en": "Your data"},
    "rights_intro": {
        "it": "Puoi chiedere una copia dei tuoi dati, una correzione o la cancellazione. La clinica esamina ogni richiesta.",
        "en": "You can ask for a copy of your data, a correction, or erasure. The clinic reviews every request.",
    },
    "rights_kind_access": {"it": "Vedere i miei dati", "en": "See my data"},
    "rights_kind_export": {"it": "Copia dei miei dati", "en": "A copy of my data"},
    "rights_kind_amend": {"it": "Correggere i miei dati", "en": "Correct my data"},
    "rights_kind_erasure": {"it": "Cancellare i miei dati", "en": "Erase my data"},
    "rights_detail_label": {"it": "Dettagli (facoltativo)", "en": "Details (optional)"},
    "rights_submit": {"it": "Invia la richiesta", "en": "Send request"},
    "rights_status_open": {"it": "In attesa", "en": "Waiting for review"},
    "rights_status_approved": {"it": "Approvata", "en": "Approved"},
    "rights_status_rejected": {"it": "Respinta", "en": "Refused"},
    "rights_status_done": {"it": "Completata", "en": "Done"},
    "rights_download": {"it": "Scarica la copia (24 ore)", "en": "Download the copy (24 hours)"},
    "rights_erasure_note": {
        "it": "Alcuni dati, come le fatture, possono dover essere conservati per legge: in quel caso la clinica ti dirà quali e perché.",
        "en": "Some data, such as invoices, may have to be kept by law: if so the clinic will tell you which and why.",
    },

    "brand_line": {
        "it": "I tuoi dati clinici, quando ti servono.",
        "en": "Your clinical records, when you need them.",
    },
    "show_pin": {
        "it": "Mostra il PIN",
        "en": "Show PIN",
    },
    "hide_pin": {
        "it": "Nascondi il PIN",
        "en": "Hide PIN",
    },
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
    # two bodies because one sentence cannot be true for both paths: the forced
    # change gates the patient, the voluntary one does not, so "prima di
    # continuare" is simply false when they chose to come here from the menu
    "change_body_voluntary": {
        "it": "Scegli un nuovo PIN per il tuo account.",
        "en": "Choose a new PIN for your account.",
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
    # --- the agent (P10). every one of these is shown INSTEAD of a model
    # answer, never alongside one: the action path never calls a model.
    "agent_heading": {"it": "Appuntamenti", "en": "Appointments"},
    "agent_need_day": {
        "it": "Per quale giorno? Scrivilo come 24/10/2026.",
        "en": "Which day? Write it as 24/10/2026.",
    },
    "agent_need_period": {
        "it": "Mattina o pomeriggio?",
        "en": "Morning or afternoon?",
    },
    "agent_need_which": {
        "it": "Quale appuntamento? Rispondi con il numero.",
        "en": "Which appointment? Reply with the number.",
    },
    "agent_past_day": {
        "it": "Quel giorno è già passato. Per quale giorno vuoi l'appuntamento?",
        "en": "That day has already passed. Which day would you like?",
    },
    "agent_propose_book": {
        "it": "Richiedo un appuntamento per {day}, {period}. Confermi? Scrivi «confermo».",
        "en": "I'll request an appointment for {day}, {period}. Confirm? Reply “confirm”.",
    },
    "agent_propose_cancel": {
        "it": "Annullo l'appuntamento del {day} alle {time} con {dentist}. "
              "Confermi? Scrivi «confermo».",
        "en": "I'll cancel your appointment on {day} at {time} with {dentist}. "
              "Confirm? Reply “confirm”.",
    },
    "agent_done_book": {
        "it": "Richiesta inviata per {day}, {period}. Non è ancora un appuntamento fissato: "
              "lo studio conferma il giorno e l'ora e lo vedrai in «I tuoi appuntamenti».",
        "en": "Request sent for {day}, {period}. It is not a booking yet: the clinic confirms "
              "the day and time, and you'll see it under “Your appointments”.",
    },
    "agent_done_cancel": {
        "it": "Appuntamento del {day} alle {time} annullato.",
        "en": "Your appointment on {day} at {time} is cancelled.",
    },
    "agent_cancelled": {
        "it": "Va bene, non ho fatto nulla.",
        "en": "All right, I haven't done anything.",
    },
    "agent_nothing": {
        "it": "Non risulta nessun appuntamento da annullare.",
        "en": "There is no appointment to cancel.",
    },
    "agent_stale": {
        "it": "L'ho già fatto. Controlla «I tuoi appuntamenti».",
        "en": "I've already done that. Check “Your appointments”.",
    },
    "agent_failed_book": {
        "it": "Non sono riuscito a inviare la richiesta. Riprova o chiama lo studio.",
        "en": "I couldn't send that request. Try again, or call the clinic.",
    },
    "agent_failed_cancel": {
        "it": "Quell'appuntamento non è più annullabile. Controlla «I tuoi appuntamenti».",
        "en": "That appointment can no longer be cancelled. Check “Your appointments”.",
    },
    "agent_period_morning": {"it": "mattina", "en": "morning"},
    "agent_period_afternoon": {"it": "pomeriggio", "en": "afternoon"},
    # --- handoff (P10.05). NO ETA, ever. either the clinic is open, or the
    # next opening time is stated as a fact about the hours - never a promise
    # about when someone will reply.
    "handoff_heading": {"it": "Ti richiamiamo", "en": "We'll get back to you"},
    "handoff_body": {
        "it": "Ho segnalato la tua richiesta allo studio.",
        "en": "I've passed your request to the clinic.",
    },
    "handoff_open": {
        "it": "Lo studio è aperto adesso.",
        "en": "The clinic is open now.",
    },
    "handoff_closed": {
        "it": "Lo studio adesso è chiuso; riapre {when}.",
        "en": "The clinic is closed now; it opens again {when}.",
    },
    "handoff_closed_unknown": {
        "it": "Lo studio adesso è chiuso.",
        "en": "The clinic is closed now.",
    },
    "handoff_urgent": {
        "it": "Se è urgente chiama lo studio.",
        "en": "If it's urgent, please call the clinic.",
    },
    "handoff_status": {
        "it": "Richiesta di contatto in attesa",
        "en": "Call-back request waiting",
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
    # the invoice answer (chat.invoice_answer). billed, never "owed": there is
    # no payment status to know what is owed from (P02.03)
    "inv_on_record": {"it": "Fatture registrate: {lines}.", "en": "Invoices on record: {lines}."},
    "inv_billed_total": {"it": "Totale fatturato: {total}.", "en": "Total billed: {total}."},
    "inv_not_recorded": {
        "it": "La clinica non ha ancora registrato se queste fatture sono state pagate, quindi non posso dirti quanto resta da pagare: per questo chiedi alla clinica.",
        "en": "The clinic has not yet recorded whether these invoices were paid, so I cannot tell you what is still to pay: please ask the clinic.",
    },
    "inv_some_unknown": {
        "it": "Per alcune fatture la clinica non ha ancora registrato il pagamento: non sono incluse in questo importo.",
        "en": "For some invoices the clinic has not yet recorded payment: they are not included in that amount.",
    },
    "inv_outstanding": {
        "it": "Secondo i pagamenti registrati dalla clinica, restano da pagare {total}.",
        "en": "According to the payments the clinic has recorded, {total} is still to pay.",
    },
    "inv_next_installment": {
        "it": "Prossima rata: {amount} entro il {date}.",
        "en": "Next installment: {amount} by {date}.",
    },
    "inv_settled": {
        "it": "Secondo i pagamenti registrati dalla clinica, non resta nulla da pagare.",
        "en": "According to the payments the clinic has recorded, nothing is left to pay.",
    },

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
