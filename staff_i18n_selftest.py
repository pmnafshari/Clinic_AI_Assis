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

# the pages converted so far; the last stage makes this every template
CONVERTED = ["base.html", "_sidebar.html", "_topbar.html", "patients_new.html",
             "patients_list.html", "patients_detail.html", "documents.html", "document_detail.html", "_patient_candidates.html",
             "_visit_edit_form.html", "_patient_files.html", "_patient_pin.html", "_patient_pin_revoked.html",
             "_confirm_diff.html", "_edit_form.html"]
# modules that say nothing to a person outside _()
CONVERTED_PY = ["patients_routes.py", "document_routes.py"]
# words that stay as they are in both languages
ALLOW = {"Dental AI", "English", "Italiano", "OK"}
# technical terms that are the same in both languages
TERMS = re.compile(r"\b(?:sha256|EUR|PDF|PNG|JPEG|OPG|EXIF|CSV|JSON|PIN|ID)\b")
READ_ATTRS = ("aria-label", "placeholder", "title", "alt", "data-noun", "data-search", "data-label")


def _literals_in(text):
    """the first string argument of every _( / _html( call"""
    out = []
    for m in re.finditer(r"\b_(?:html)?\(\s*(\"(?:[^\"\\\\]|\\\\.)*\"|'(?:[^'\\\\]|\\\\.)*')", text):
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
    return found


def all_texts():
    found = set()
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
        e = re.sub(r"\b_(?:html)?\(\s*(\"(?:[^\"\\\\]|\\\\.)*\"|'(?:[^'\\\\]|\\\\.)*')", "_()", e)
        for m in re.finditer(r"\"([^\"]*)\"|'([^']*)'", e):
            lit = m.group(1) or m.group(2) or ""
            # words joined to an expression with ~ are a sentence in pieces, whatever they look like
            joined = re.search(r"~\s*$", e[:m.start()]) or re.match(r"\s*~", e[m.end():])
            if joined and re.search(r"[A-Za-z]{2,}", lit) and lit.strip() not in ALLOW and not re.match(r"^[#/?&]", lit):
                prose.append(lit)
                continue
            tokens = lit.split()
            if lit in ALLOW or not tokens:
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
            called = [p.string for p in prev[-2:]] == ["_", "("]
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
        for user, role in (("dentist", "dentist"), ("assistant", "assistant")):
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
                                 (d, "/patients/ZZIT800101010101/documents", ("Cronologia dei file", "Due decisioni separate", "Carica"))):
            page = who.get(path).get_data(as_text=True)
            for word in words:
                assert word in page, f"4: {word!r} is not on the Italian {path}"
        c.get("/lang/en?next=/")
        assert '<html lang="en">' in c.get("/patients/new").get_data(as_text=True), "4: English does not come back"
        login = app.test_client().get("/login").get_data(as_text=True)
        assert 'href="/lang/it?next=' in login, "4: the sign-in page has no language switch"
        conn.close()
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        print("usage: python staff_i18n_selftest.py --selftest")
