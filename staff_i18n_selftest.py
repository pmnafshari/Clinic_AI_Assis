"""P29 E-001 (owner, 2026-11-04): the staff app in English and Italian.

Every text written as _("...") has its Italian; a converted page has no text left outside _() - visible text, the
attributes a person reads, and prose strings inside template expressions; a converted module flashes only through
_(); the switch keeps the page and never leaves the app; and the converted pages render in Italian.
"""
import ast
import re
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path

from werkzeug.security import generate_password_hash

import patient_id
import storage

ROOT = Path(__file__).parent
TEMPLATES = ROOT / "app" / "templates"

# every page and every module of the staff app
CONVERTED = sorted(p.name for p in TEMPLATES.glob("*.html"))
# modules that say nothing to a person outside _() - the catalog and i18n.py itself aside
CONVERTED_PY = sorted(p.name for p in (ROOT / "app").glob("*.py") if p.name not in ("strings_it.py", "i18n.py"))
# words that stay as they are in both languages
ALLOW = {"Dental AI", "English", "Italiano", "OK"}
# technical terms that are the same in both languages
TERMS = re.compile(r"\b(?:sha256|EUR|PDF|PNG|JPEG|OPG|EXIF|CSV|JSON|PIN|ID)\b")
READ_ATTRS = ("aria-label", "placeholder", "title", "alt", "data-noun", "data-search", "data-label")


def _literals_in(text):
    """the first string argument of every _( / _html( call"""
    out = []
    for m in re.finditer(r"\b(?:N_|_html|_)\(\s*(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')", text):
        out.append(ast.literal_eval(m.group(1)))
    return out


def _errors_in(path, names=("DocumentError",)):
    """the message of every raise SomeError("code", "message") in a module"""
    out = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) in names:
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                out.append(node.args[1].value)
    return out


def _sentence(node):
    """a string constant, or an f-string as the catalog writes it: each value a placeholder {a}, {b}, ... in order"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out, n = [], 0
        for part in node.values:
            if isinstance(part, ast.Constant):
                out.append(part.value)
            else:
                out.append("{" + "abcdefgh"[n] + "}")
                n += 1
        return "".join(out)
    return None


def _messages_in(path, function=None):
    """what a module raises as ValueError, or what one function returns, as sentences"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    if function:
        tree = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == function)
    out = []
    for node in ast.walk(tree):
        if function and isinstance(node, ast.Return) and node.value is not None:
            out.append(_sentence(node.value))
        if not function and isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) \
                and getattr(node.exc.func, "id", "") in ("ValueError", "LedgerError", "StockError") and node.exc.args:
            out.append(_sentence(node.exc.args[0]))
    return {m for m in out if m}


def _raised(path, names):
    """what a module raises as one of these errors: the message is the last argument - (code, message) or (message)"""
    out = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and node.exc.args \
                and getattr(node.exc.func, "attr", getattr(node.exc.func, "id", "")) in names:
            out.add(_sentence(node.exc.args[-1]))
    return {m for m in out if m}


def _returns_of(path, function):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.FunctionDef) and node.name == function:
            return [r.value.value for r in ast.walk(node) if isinstance(r, ast.Return) and isinstance(r.value, ast.Constant)
                    and isinstance(r.value.value, str)]
    return []


def harvest():
    """words other modules hand to the converted pages, shown through _(value)"""
    import patient_files
    import patient_identity
    found = set(patient_files.CATEGORIES.values()) | set(patient_files.FILE_TYPES.values()) | set(patient_files.DATE_BASIS.values())
    found |= {"Image", "PDF document", "Text document", "File", "acquired", "imported", "uploaded", "visit date", "Visit note",
              "Note", "legacy import, batch {v}", "uploaded by {v}", "confirmed by {v}", "someone not recorded"}
    found |= set(_errors_in(ROOT / "documents.py")) | set(_errors_in(ROOT / "patient_files.py"))
    found |= set(_returns_of(ROOT / "documents.py", "_refusal"))
    found |= {"pending review", "quarantined", "extraction failed", "confirmed", "rejected", "superseded"}
    found |= {"Visit", "Appointment", "Billing", "Record", "Filed", "Booked", "Requested", "Cancelled", "Declined", "Merged",
              "from a filed note", "scheduling", "record merge", "Appointment booked", "Requested an appointment",
              "Appointment cancelled", "Request declined", "with {dentist}", "Draft invoice {amount} EUR", "Billed {amount} EUR",
              "Absorbed the record of {who}", "{cf}, merged by {who}"}
    found |= set(patient_identity.BILL_STATE_WORDS.values()) | set(patient_identity.BILL_STATE_SHORT.values())
    found |= {"draft", "active", "cancelled", "superseded", "patient"}
    # stage 3: appointments, requests and Home
    import appointments
    import imaging_bookings
    from app import dashboard_routes, reasons
    from app.appointments_routes import STATUS_WORDS
    found |= _messages_in(ROOT / "appointments.py") | _messages_in(ROOT / "availability.py", "refusal")
    found |= {appointments.SLOT_TAKEN, "the clinic is closed", "away"}
    found |= {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"}
    for one, many, word in STATUS_WORDS.values():
        found |= {f"{{n}} {one}", f"{{n}} {many}", word, f"{{n}} {word}", f"nothing {word}"}
    found |= {"morning", "afternoon", "booked", "requested", "declined"}
    found |= set(reasons.DECLINE) | set(reasons.REJECT_NOTE) | set(reasons.STOCK) | set(reasons.REFUND) | set(reasons.REVERSE)
    found |= set(dashboard_routes._TOOL_WORDS.values()) | set(dashboard_routes._FIELD_WORDS.values())
    found |= set(dashboard_routes._STATE_WORDS.values()) | {"Record changed"}
    found |= set(imaging_bookings.FLAGS.values())
    # stage 4: billing, stock, call-backs, reminders, data requests, reports
    import handoff
    import inventory_job
    import ledger
    import reminder_job
    import reminders
    from app.reports_routes import PERIODS
    found |= _messages_in(ROOT / "ledger.py") | _messages_in(ROOT / "inventory.py")
    found |= {"issued", "partially paid", "paid", "void", "cash", "card", "bank transfer", "other", "manual", "reconciliation",
              "demo fixture", "provider", ledger.ALLOCATION_ORDER, "Kept in full", "Fully refunded", "Reversed", "Refund", "Reversal"}
    found |= {inventory_job.SCHEDULE, reminder_job.SCHEDULE, reminders.DisabledTransport.reason, "ok", "failed", "never"}
    for status in ("scheduled", "claimed", "sent", "delivered", "failed", "cancelled"):
        found |= {status, status.capitalize()}
    found |= {"appointment", "invoice", "installment"} | {r.replace("_", " ") for r in reminders.CANCEL_REASONS + reminders.ERRORS}
    found |= set(handoff.TOPICS) | {"access", "export", "amend", "erasure", "open", "approved", "rejected", "done"}
    found |= {"invoices kept under the retention policy (demo template: {a})"} | {v[1] for v in PERIODS.values()}
    # stage 5: the rest - records Q&A, providers, guides, imaging, legacy import, summaries, similar cases, reviews,
    # Jarvis, the command box, accounts and dictation
    import agent
    import clinic_guides
    import delivery
    import dictation
    import drive_source
    import imaging_bookings
    import imaging_requests
    import legacy_import
    import providers
    import similar_cases
    import visit_summary
    import web_auth
    from app import import_routes
    found |= {"not in records", "not recorded", "invalid codice fiscale", "couldn't identify a patient in that question",
              "no patient named {name} on record", "no patient on record with codice fiscale {cf}",
              "Ollama not reachable - run: ollama run {model}", "the appointment book", "the visit of {date}",
              "{name}, visit of {date}", "an undated visit",
              "{name}'s phone: {value} (no visit on record to cite)", "{name}'s phone: {value}",
              "{name} has no invoices on record (no visit on record to cite)", "{name} has no invoices on record",
              "{name}: billed {billed}, still to pay {left} ({n} invoice(s) not yet reconciled, not counted) (no visit on record to cite)",
              "{name}: billed {billed}, still to pay {left} ({n} invoice(s) not yet reconciled, not counted)",
              "{name}: billed {billed}, still to pay {left} (no visit on record to cite)",
              "{name}: billed {billed}, still to pay {left}",
              "{name}'s next appointment: {when} with {dentist} (booked); the latest note's recall says: {recall}",
              "{name}'s next appointment: {when} with {dentist} (booked)",
              "{name} has no booked appointment; the latest note's recall says: {recall}", "{name} has no booked appointment",
              "{name}'s last visit date: {value} (no visit on record to cite)", "{name}'s last visit date: {value}",
              "{name}: unrecognized field {field} (no visit on record to cite)", "{name}: unrecognized field {field}"}
    found |= {k.capitalize() for k in providers.KINDS} | set(providers.KINDS)
    found |= {r.replace("_", " ") for r in providers.REASONS} | {providers.DISABLED, providers.SANDBOX, "unknown (refused)"}
    found |= {"call event refused", "call ended", "call started", "link created", "redirect seen", "webhook refused",
              "webhook duplicate", "webhook accepted", "blocked", "kill on", "kill off"}
    found |= {o.capitalize() for o in delivery.OUTCOMES}
    found |= _raised(ROOT / "clinic_guides.py", ("GuideError",)) | set(clinic_guides.ESCALATE.values())
    found |= set(clinic_guides.MESSAGES.values()) | {"withdrawn", "approved", "uploaded", "approve refused",
                                                     "approver named", "approver revoked", "restricted", "by source {a}", "pages {a}"}
    found |= _raised(ROOT / "imaging_requests.py", ("RequestError",)) | {v for v in imaging_requests.EXAMS.values() if v}
    found |= set(imaging_requests.EVENT_WORDS.values()) | {imaging_requests.NO_REQUEST, "replaced by version {a}",
                                                           "No approved procedure covers this type."}
    found |= {"This request is a draft: the dentist has not activated it. There is nothing to do; ask the dentist.",
              "This request was cancelled by {a}. There is nothing to do; ask the dentist if in doubt.",
              "This version was superseded by a later one. Open the current version; do not act on this one."}
    found |= _raised(ROOT / "imaging_bookings.py", ("BookingError", "RequestError")) | set(imaging_bookings.RESOLUTION_LABELS.values())
    found |= {"Booked for {a}.", "Done - recorded by {a} on {b}.", "Appointment {a} needs reception action: {b}.", "Not booked."}
    found |= set(import_routes.STATE_LABELS.values()) | set(import_routes.STRENGTH.values())
    found |= set(drive_source.STATUS_LABELS.values()) | {"manual", "auto"}
    found |= _raised(ROOT / "legacy_import.py", ("ImportProblem",)) | _raised(ROOT / "drive_source.py", ("DriveError",))
    found |= {"image", "pdf", "text", "dicom", "strong", "partial", "weak", "camera time", "file date", "document date"}
    found |= _raised(ROOT / "visit_summary.py", ("SummaryError",)) | {visit_summary.DRAFT_LABEL, "generated", "edited"}
    found |= {"draft", "approved", "rejected", "superseded", "the patient record", dictation.TTS_UNAVAILABLE, dictation.STT_UNAVAILABLE}
    found |= {similar_cases.OFF_MESSAGE, similar_cases.CRITERIA_NOTE, similar_cases.CRITERIA["require"], "no outcome recorded",
              "same recorded procedure code: {a}", "same tooth: {a}", "same quadrant: {a}", "shared note words: {a}", "case {a}",
              "similar", "not similar", "This visit records no procedure code, so there is nothing to compare.",
              "Only {a} reviewed case(s) from other patients - too few to compare.",
              "{a} of {b} reviewed cases from other patients share a recorded procedure code.",
              "No similar case among {a} reviewed cases from other patients."}
    found |= _raised(ROOT / "note_review.py", ("ReviewError",)) | {"pending", "extraction failed", "Pending", "Confirmed", "Rejected"}
    found |= _raised(ROOT / "jarvis_link.py", ("LinkError",)) | _raised(ROOT / "dictation.py", ("Unavailable", "Unclear", "NotPrivate"))
    found |= _messages_in(ROOT / "agent.py") | set(agent.EDITABLE_FIELDS) | {"empty"}
    found |= {text for _key, text in web_auth.PASSWORD_RULES}
    found |= {"no folder is configured (CLINIC_DRIVE_FOLDER_ID)", "no API key: put it in {a} (outside the repository)"}
    # the command box says the dictation sentence with "the command" for "the note" (agent_routes._dictation)
    found |= {m.replace("type the note", "type the command") for m in _raised(ROOT / "dictation.py", ("Unavailable",))
              | {dictation.STT_UNAVAILABLE} if "type the note" in m}
    found |= {"{a} created.", "Role changed to {a}.", "Account {a}.", "Lockout cleared.", "Enter a username.",
              "role must be one of {a}", "Enter a password for the new account.", "no such user",
              "That username is already taken.", "not permitted: cannot change your own account", "active", "disabled"}
    return found


def all_texts():
    found = {"appointment|booked", "appointment|cancelled", "appointment|requested", "feature|on", "feature|off"}
    files = [TEMPLATES / name for name in CONVERTED] + [ROOT / "app" / name for name in CONVERTED_PY + ["nav.py", "patients_routes.py"]]
    for f in files + [ROOT / "patient_identity.py"]:
        found.update(_literals_in(f.read_text(encoding="utf-8")))
    import app.nav as nav
    for group, label, *_rest in nav.ITEMS:
        found.update((group, label))
    for one, many in nav.COUNT_NOUNS.values():
        found.update((f"{{n}} {one}", f"{{n}} {many}"))
    for _key, _icon, (one, many), detail, _path, _warn in nav.NOTIFY:
        found.update((f"{{n}} {one}", f"{{n}} {many}", detail))
    import patient_identity
    found.update(patient_identity.DOCUMENTS)
    found.update(harvest())
    import app.i18n as i18n
    found.update(k[3] for k in list(i18n.FLASH_KINDS.values()) + [i18n.FLASH_NOTE])
    found.update(i18n.ROLES)
    found.update(i18n.JS_TEXT)
    # the field problems register() returns are shown through _(problems[name])
    found.update(re.findall(r'(?:problems\["\w+"\] =|\{"codice_fiscale":) "([^"]+)"', (ROOT / "patient_identity.py").read_text()))
    found.update(r for r in ("the same name", "nearly the same name", "the same phone number",
                             "two letters of the codice fiscale swapped",
                             "the same digits in the codice fiscale, different letters",
                             "a codice fiscale differing in at most two places"))
    return found


class _Loose(HTMLParser):
    def __init__(self):
        super().__init__()
        self.loose, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        for name, value in attrs:
            if name in READ_ATTRS and value and re.search(r"[A-Za-z]{2,}", value) and value.strip() not in ALLOW:
                self.loose.append(f"{tag}[{name}]={value!r}")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip -= 1

    def handle_data(self, data):
        words = " ".join(TERMS.sub("", data).split())
        # a macro that writes attributes leaves name="value" fragments: markup, not words a person reads
        if not self.skip and re.search(r"[A-Za-z]{2,}", words) and words not in ALLOW and '="' not in words:
            self.loose.append(repr(words))


def loose_text(src):
    """text a person reads that is not inside _(): visible text and reading attributes (with every template block
    taken out first), plus prose string literals inside template expressions"""
    src = re.sub(r"\{#.*?#\}", " ", src, flags=re.S)
    exprs = re.findall(r"\{\{.*?\}\}|\{%.*?%\}", src, flags=re.S)
    prose = []
    for e in exprs:
        e = re.sub(r"\b_(?:html)?\(\s*(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')", "_()", e)
        for m in re.finditer(r"\"([^\"]*)\"|'([^']*)'", e):
            lit = m.group(1) or m.group(2) or ""
            # words joined to an expression with ~ are a sentence in pieces, whatever they look like
            joined = re.search(r"~\s*$", e[:m.start()]) or re.match(r"\s*~", e[m.end():])
            # an id prefix ('dreason-' ~ r.id) or a link part is markup, not words
            if joined and re.search(r"[A-Za-z]{2,}", lit) and lit.strip() not in ALLOW and not re.match(r"^[#/?&]", lit) \
                    and not re.fullmatch(r"[a-z0-9-]+-", lit):
                prose.append(lit)
                continue
            tokens = lit.split()
            if lit in ALLOW or not tokens or re.search(r"(==|!=|\bin|startswith\(|endswith\()\s*$", e[:m.start()]):
                continue
            if any(re.search(r"[-_./:%#=<>{}()]", t) for t in tokens) and not re.match(r"^[A-Z][a-z]+ ", lit):
                continue
            if (len(tokens) > 1 and re.search(r"[A-Za-z]{3}", lit)) or re.match(r"^[A-Z][a-z]{2,}", lit):
                prose.append(lit)
    parser = _Loose()
    parser.feed(re.sub(r"\{\{.*?\}\}|\{%.*?%\}", " ", src, flags=re.S))
    return parser.loose + [f"expr {p!r}" for p in prose]


def loose_python(src):
    import io
    import tokenize
    out, prev = [], []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.STRING and not tok.string.lstrip("rbfRBF").startswith(('"""', "'''")):
            text = ast.literal_eval(tok.string) if not tok.string.lstrip("rbRB").startswith(("f", "F")) else tok.string
            called = [p.string for p in prev[-2:]] in (["_", "("], ["N_", "("])
            if not called and re.match(r"^f?[\"']?[A-Z][a-z]+[^\n]* [^\n]*[.!?:]$", str(text).strip("\"'")):
                out.append(str(text)[:70])
        if tok.type not in (tokenize.NL, tokenize.COMMENT):
            prev.append(tok)
    return out


def selftest():
    sys.path.insert(0, str(ROOT))
    from app.strings_it import IT

    # 1. every text written for translation has its Italian, with the same placeholders
    texts = all_texts()
    missing = sorted(t for t in texts if not IT.get(t))
    assert not missing, f"1: {len(missing)} texts without Italian, e.g. {missing[:8]}"
    for en, it in IT.items():
        assert set(re.findall(r"\{\w+\}", en)) == set(re.findall(r"\{\w+\}", it)), f"1: placeholders differ: {en!r} / {it!r}"
        # _(), _html() and template_text() take the sentence as "text": a value of that name never reaches it
        assert "{text}" not in en, f"1: {{text}} cannot be a placeholder: {en!r}"

    # 1b. no app module binds _ (a throwaway "_, x = ..." silently replaces the translation in that function)
    for f in (ROOT / "app").glob("*.py"):
        bound = [l.strip() for l in f.read_text(encoding="utf-8").splitlines()
                 if re.search(r"(^|[ (,])_\s*(=[^=]|,)|\bfor _\b|\bfor _," , l) and "import" not in l]
        assert not bound, f"1b: {f.name} binds _: {bound[:2]}"

    # 1c. a module's sentence comes out whole: its own catalog line, or a pattern built for it - never a short frame
    # ("{a} is {b} on {c}") that a whole sentence happens to fit, which would leave it half translated
    import flask
    import app.i18n as i18n_c
    with flask.Flask("c").test_request_context(headers={"Cookie": "staff_lang=it"}):
        loose = "The printer is out of paper for two days on the second floor"
        assert i18n_c.message(loose) == loose, f"1c: a short frame took a whole sentence: {i18n_c.message(loose)!r}"
        assert i18n_c.message("Mario Rossi's phone: 340 1234567") == "telefono di Mario Rossi: 340 1234567", "1c: an answer pattern"
        assert i18n_c.message("Approved by drossi") == "Approvata da drossi", "1c: a short frame with a short value"

    # 2. a converted page leaves no text outside _()
    for name in CONVERTED:
        left = loose_text((TEMPLATES / name).read_text(encoding="utf-8"))
        assert not left, f"2: {name} has text outside _(): {left[:6]}"

    # 3. a converted module says nothing to a person outside _(): no message-like string literal (a capital, a space,
    # ending in . ! ? or :) that is not the argument of _()
    for name in CONVERTED_PY:
        left = loose_python((ROOT / "app" / name).read_text(encoding="utf-8"))
        assert not left, f"3: {name} has messages outside _(): {left[:4]}"

    # 4. the switch keeps the page, never leaves the app, and the pages answer in Italian
    with tempfile.TemporaryDirectory() as tmp:
        import app.db as app_db
        import clinic_guides
        from app import create_app
        db = Path(tmp) / "i18n.sqlite"
        conn = storage.init_db(str(db))
        for user, role in (("dentist", "dentist"), ("assistant", "assistant"), ("boss", "admin")):
            conn.execute("INSERT INTO users (username, password_hash, role, active) VALUES (?, ?, ?, 1)",
                         (user, generate_password_hash(f"{user}-pw-123"), role))
        patient_id.seed_patient(conn, "ZZIT800101010101", "Ilaria Italiana", "+393401230000")
        conn.commit()
        app_db.DB_PATH = str(db)
        app_db.CHROMA_PATH = str(Path(tmp) / "chroma")
        clinic_guides.DB_PATH = str(Path(tmp) / "guides.sqlite")
        clinic_guides.STORE = Path(tmp) / "guides"
        app = create_app()
        app.config["WTF_CSRF_ENABLED"] = False
        c = app.test_client()
        assert c.post("/login", data={"username": "assistant", "password": "assistant-pw-123"}).status_code in (302, 303)
        r = c.get("/lang/it?next=/patients/new")
        assert r.status_code == 302 and r.headers["Location"].endswith("/patients/new"), f"4: the switch lost the page {r.headers}"
        assert "staff_lang=it" in r.headers.get("Set-Cookie", ""), "4: the switch did not keep the choice"
        for evil in ("//elsewhere.example/x", "https://elsewhere.example/", "/\\elsewhere.example", ""):
            loc = c.get("/lang/it", query_string={"next": evil}).headers["Location"]
            assert loc.endswith("/") and "elsewhere" not in loc, f"4: the switch leaves the app for {evil!r}: {loc}"
        page = c.get("/patients/new").get_data(as_text=True)
        assert '<html lang="it">' in page, "4: the page does not say it is Italian"
        for word in ("Nuovo paziente", "Pazienti", "Esci", "Codice fiscale"):
            assert word in page, f"4: {word!r} is not on the Italian page"
        bad = c.post("/patients/new", data={"patient_name": "", "codice_fiscale": "", "phone": "", "document": ""}).get_data(as_text=True)
        assert "Scrivi il codice fiscale" in bad, "4: a field's problem is said in English on the Italian page"
        done = c.post("/patients/new", data={"patient_name": "Nuova Persona", "codice_fiscale": "ZZNP800101010102",
                                             "phone": "3401112222", "document": "health card"}, follow_redirects=True)
        assert "Registrazione completata" in done.get_data(as_text=True), "4: the result says it in English"
        # stage 2: the patients list, a record and its files, in Italian
        c.get("/lang/it?next=/")
        d = app.test_client()
        assert d.post("/login", data={"username": "dentist", "password": "dentist-pw-123"}).status_code in (302, 303)
        d.get("/lang/it?next=/")
        for who, path, words in ((c, "/patients", ("Filtra questo elenco", "Nuovo paziente", "Prossimo appuntamento")),
                                 (c, "/patients/ZZIT800101010101", ("Panoramica", "Dati anagrafici", "Cronologia della cartella")),
                                 (d, "/patients/ZZIT800101010101/documents", ("Cronologia dei file", "Due decisioni separate", "Carica")),
                                 (c, "/appointments?view=week", ("Settimana del", "Richieste dei pazienti", "Giorno")),
                                 (c, "/appointments?view=month", ("Scegli un giorno per aprire", "Mese precedente")),
                                 (c, "/requests", ("Richieste di appuntamento", "Lavoro di imaging")),
                                 (c, "/", ("Azioni rapide", "La tua attività recente")),
                                 (c, "/billing", ("Fatture e pagamenti", "Ancora da pagare")),
                                 (c, "/stock", ("Materiali di consumo", "Articoli")),
                                 (c, "/handoffs", ("Cos'è una richiamata", "La coda")),
                                 (c, "/reminders", ("Coda d'invio", "L'invio è spento")),
                                 (d, "/data-requests", ("Nessuna richiesta sui dati",)),
                                 (d, "/reports", ("Cosa questa pagina può e non può mostrare", "Tutta la clinica"))):
            page = who.get(path).get_data(as_text=True)
            for word in words:
                assert word in page, f"4: {word!r} is not on the Italian {path}"
        # stage 5: the rest of the pages, a records answer and the page scripts, in Italian
        a = app.test_client()
        assert a.post("/login", data={"username": "boss", "password": "boss-pw-123"}).status_code in (302, 303)
        a.get("/lang/it?next=/")
        for who, path, words in ((d, "/qa", ("Fai una domanda", "Esempi")),
                                 (d, "/providers", ("Interruttori", "Linea telefonica", "Messaggi")),
                                 (d, "/guides", ("Biblioteca delle guide",)),
                                 (d, "/guides/ask", ("Chiedi alle guide", "La tua domanda")),
                                 (d, "/imports", ("Importazione archivio", "L'importazione non allega nulla")),
                                 (d, "/reviews", ("Note da revisionare",)),
                                 (d, "/patients/ZZIT800101010101/summary", ("Riepilogo per la prossima visita", "Ancora nessun riepilogo")),
                                 (d, "/notes/new", ("Scrivi o incolla la nota",)),
                                 (d, "/agent/command", ("Esegui un comando",)),
                                 (d, "/jarvis/link", ("Usa Jarvis su questo dispositivo",)),
                                 (c, "/imaging/tasks", ("Richiede un intervento della reception",)),
                                 (d, "/change-password", ("Password attuale", "Nascondi la password attuale")),
                                 (a, "/admin/users", ("Account del personale",)),
                                 (a, "/patients/duplicates", ("Possibili duplicati",))):
            page = who.get(path).get_data(as_text=True)
            for word in words:
                assert word in page, f"4: {word!r} is not on the Italian {path}"
        # a records answer is built in English by ask.py and said in Italian; so is a no-record answer
        said = d.post("/qa", data={"question": "What is patient Ilaria Italiana's phone number?"}).get_data(as_text=True)
        assert "telefono di Ilaria Italiana" in said.lower() or "telefono di ilaria italiana" in said.lower(), "4: the answer is in English"
        none = d.post("/qa", data={"question": "What is patient Zeno Nessuno's phone number?"}).get_data(as_text=True)
        assert "Nessuna cartella trovata" in none and "Nessun paziente di nome" in none, "4: the no-record answer is in English"
        # the scripts get their sentences in Italian, and nothing in English
        assert '"Loading...": "Caricamento..."' in d.get("/qa").get_data(as_text=True), "4: the scripts get no Italian"
        c.get("/lang/en?next=/")
        assert '<script type="application/json" id="ui-text">{}</script>' in c.get("/").get_data(as_text=True), "4: English pages carry a catalog"
        assert '<html lang="en">' in c.get("/patients/new").get_data(as_text=True), "4: English does not come back"
        login = app.test_client().get("/login").get_data(as_text=True)
        assert 'href="/lang/it?next=' in login, "4: the sign-in page has no language switch"
        conn.close()

    # 5. the page scripts say only what i18n.JS_TEXT lists, and it lists only what they say; no script writes words
    # a person reads outside uiText()
    import app.i18n as i18n
    said = set()
    for name in ("app.js", "list_tools.js", "look.js", "login_return.js"):
        src = (ROOT / "app" / "static" / "js" / name).read_text(encoding="utf-8")
        said |= set(re.findall(r'uiText\("([^"]*)"', src))
        loose = re.findall(r'(?:textContent|placeholder|title|alt)\s*=\s*(?:[^;]*\?\s*)?["`][A-Z][a-z]|'
                           r'setAttribute\("aria-label",\s*["`][A-Za-z]|\bel\([^)]*,\s*"[A-Z][a-z]|dropdown\("[A-Z]', src)
        assert not loose, f"5: {name} writes words outside uiText(): {loose[:3]}"
    assert said == set(i18n.JS_TEXT), f"5: JS_TEXT and the scripts differ: {sorted(said ^ set(i18n.JS_TEXT))}"
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        print("usage: python staff_i18n_selftest.py --selftest")
