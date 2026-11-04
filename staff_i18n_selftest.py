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
CONVERTED = ["base.html", "_sidebar.html", "_topbar.html", "patients_new.html"]
# modules whose every flash is translated
CONVERTED_PY = []
# words that stay as they are in both languages
ALLOW = {"Dental AI", "English", "Italiano", "OK"}
READ_ATTRS = ("aria-label", "placeholder", "title", "alt")


def _literals_in(text):
    """the first string argument of every _( / _html( call"""
    out = []
    for m in re.finditer(r"\b_(?:html)?\(\s*(\"(?:[^\"\\\\]|\\\\.)*\"|'(?:[^'\\\\]|\\\\.)*')", text):
        out.append(ast.literal_eval(m.group(1)))
    return out


def all_texts():
    found = set()
    for f in list(TEMPLATES.glob("*.html")) + [p for p in (ROOT / "app").glob("*.py") if p.name != "i18n.py"] + [ROOT / "patient_identity.py"]:
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
        words = " ".join(data.split())
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
        for lit in re.findall(r"\"([^\"]*)\"|'([^']*)'", e):
            lit = lit[0] or lit[1]
            tokens = lit.split()
            if lit in ALLOW or not tokens or any(re.search(r"[-_./:%#=<>{}()]", t) for t in tokens):
                continue
            if (len(tokens) > 1 and re.search(r"[A-Za-z]{3}", lit)) or re.match(r"^[A-Z][a-z]{2,}", lit):
                prose.append(lit)
    parser = _Loose()
    parser.feed(re.sub(r"\{\{.*?\}\}|\{%.*?%\}", " ", src, flags=re.S))
    return parser.loose + [f"expr {p!r}" for p in prose]


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

    # 3. a converted module flashes only through _()
    for name in CONVERTED_PY:
        src = (ROOT / "app" / name).read_text(encoding="utf-8")
        raw = [m.group(0) for m in re.finditer(r"flash\(\s*(?!_\()[^\n]{0,60}", src)]
        assert not raw, f"3: {name} flashes untranslated text: {raw[:3]}"

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
