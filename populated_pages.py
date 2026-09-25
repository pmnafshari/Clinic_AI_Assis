"""The pages the dev data leaves empty, shot again with something in them.

shot_pages.py measures every staff page against db/clinic.sqlite, where the
fixture patient has no document and no case from another patient - so the
documents list, a search hit, a document's review page and the similar-case
results were only ever measured as empty states. Rows, badges, snippets and
the per-row forms are where overflow, contrast and naming go wrong.

This starts a staff app of its own, in this process, on POPULATED_URL, over a
temporary database, documents folder and indexes, with similar cases switched
on. It is seeded through the real domain functions (the sandboxed reader,
confirm, index, replace, reject, feedback), then every page is checked at each
width for horizontal overflow, text contrast, a visible focus indicator on
every control, and a name on every control that says which row it acts on.
Each page also has to show what it was seeded with, so an empty page fails
instead of passing as a vacuous measurement. The temporary folder is the only
place anything is written; db/clinic.sqlite is never opened.

Run by shot_pages.py; standalone for one width:
    .venv/bin/python populated_pages.py [--width 390] [--out DIR]
"""
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

from werkzeug.security import generate_password_hash

POPULATED_PORT = 5003
POPULATED_URL = f"http://127.0.0.1:{POPULATED_PORT}"
USER, PASS = "zzp_dentist", "zzp_populated_1234"
CF = "ZZPP850010150802"

# every control inside <main> needs a name, a name set with aria-label has to
# contain the words on screen, and two controls with the same name must not
# lead to different places - "Similar" three times over three cases tells a
# screen-reader user nothing about which case it is
NAMES = """
() => {
  const text = (el) => (el.textContent || '').replace(/\\s+/g, ' ').trim();
  const nameOf = (el) => {
    const aria = (el.getAttribute('aria-label') || '').trim();
    if (aria) return aria;
    const by = el.getAttribute('aria-labelledby');
    if (by) return by.split(/\\s+/).map(id => text(document.getElementById(id) || {})).join(' ').trim();
    if (el.id) {
      const l = document.querySelector('label[for="' + el.id + '"]');
      if (l) return text(l);
    }
    if (el.closest('label')) return text(el.closest('label'));
    if (el.tagName === 'INPUT' && ['submit', 'button'].includes(el.type)) return (el.value || '').trim();
    return text(el) || (el.getAttribute('title') || '').trim();
  };
  const target = (el) => {
    if (el.tagName === 'A') return el.getAttribute('href') || '';
    if (!el.form) return el.getAttribute('hx-get') || el.getAttribute('data-bs-target') || '';
    const fields = [...el.form.querySelectorAll('input[type=hidden]:not([name=csrf_token])')]
      .map(i => i.name + '=' + i.value).join('&');
    return (el.form.getAttribute('action') || '') + '?' + fields;
  };
  const out = [];
  const seen = {};
  const scope = document.querySelector('main') || document.body;
  scope.querySelectorAll('a[href], button, input:not([type=hidden]), select, textarea').forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return;
    const name = nameOf(el);
    const shown = text(el);
    const what = el.tagName + ' ' + (shown || el.getAttribute('name') || '').slice(0, 30);
    if (!name) { out.push('no accessible name: ' + what); return; }
    const aria = (el.getAttribute('aria-label') || '').toLowerCase();
    if (aria && shown && !aria.includes(shown.toLowerCase())) {
      out.push('aria-label hides the visible words: ' + what + ' -> ' + aria);
    }
    const key = name.toLowerCase();
    (seen[key] = seen[key] || new Set()).add(target(el));
  });
  for (const [name, targets] of Object.entries(seen)) {
    if (targets.size > 1) out.push('same name, ' + targets.size + ' different targets: "' + name + '"');
  }
  return out;
}
"""


HIDDEN = "(i) => getComputedStyle(document.querySelector('[data-a11y-i=\"' + i + '\"]')).visibility === 'hidden'"
FOCUSED = "(i) => document.activeElement === document.querySelector('[data-a11y-i=\"' + i + '\"]')"

# the staff chrome (menu toggle, sidebar links, sign out) must show the design-system ring,
# not the browser's or Bootstrap's: a ring that is merely present can still be the wrong one.
# sign out is the one danger control, so its ring is the danger ring
CHROME_RING = """
(i) => {
  const el = document.querySelector('[data-a11y-i="' + i + '"]');
  if (!el.matches('.app-topbar-toggle, .app-side-link, .app-side-sub, .app-signout')) return true;
  const probe = document.createElement('div');
  probe.style.boxShadow = el.matches('.app-signout') ? 'var(--ds-focus-ring-danger)' : 'var(--ds-focus-ring)';
  document.body.appendChild(probe);
  const want = getComputedStyle(probe).boxShadow;
  probe.remove();
  return getComputedStyle(el).boxShadow === want;
}
"""


def _pdf(text):
    # the hand-built PDF the documents selftest uses; one page, one line
    from documents_selftest import pdf
    return pdf([text])


def seed(conn):
    """-> [(name, path, must_show)]: each page and the text proving it is not empty."""
    import documents as docs
    import note_review
    import patient_id
    import similar_cases as sc
    conn.execute("INSERT INTO users (username, password_hash, role, active) VALUES (?, ?, ?, 1)",
                 (USER, generate_password_hash(PASS), "dentist"))
    pid = patient_id.seed_patient(conn, CF, "Zzp Popolata", "3330000001")
    me = (USER, "dentist")

    def visit(patient, day, procedures, note):
        vid = conn.execute("INSERT INTO visits (patient_id, visit_date, procedures, clinical_notes,"
                           " source_path) VALUES (?, ?, ?, ?, ?)",
                           (patient, day, json.dumps(procedures), note,
                            f"populated/{patient}-{day}.json")).lastrowid
        conn.commit()
        note_review.mark_reviewed(conn, vid, "typed", USER)
        return vid

    src = visit(pid, "2026-03-01", ["rct 26", "filling 25"],
                "rct 26 prima seduta, dolore alla percussione; otturazione 25")
    visit(pid, "2026-01-10", ["ext 21"], "estrazione 21, nessun dolore riferito")
    # other patients' reviewed visits, with the identifiers the minimised view
    # has to redact, so the redaction marks are on the page being measured
    others = [("ZZPQ850010150803", "Quirino Altro", ["rct 26"],
               "rct 26 dolore percussione, chiamare 3331234567"),
              ("ZZPR850010150804", "Rosa Seconda", ["rct 26"],
               "rct 26 seconda seduta, scrive a rosa@example.org"),
              ("ZZPS850010150805", "Sergio Terzo", ["rct 27"], "rct 27 dolore, parente di Rosa Seconda"),
              ("ZZPT850010150806", "Tea Quarta", ["filling 25"], "otturazione composita sul 25")]
    cases = []
    for cf, name, procedures, note in others:
        other = patient_id.seed_patient(conn, cf, name)
        cases.append(visit(other, "2025-11-20", procedures, note))
    sc.feedback(conn, src, pid, cases[0], "similar", "same tooth", *me)

    confirmed = docs.ingest(conn, pid, _pdf("referto otturazione dente 25 composito"), "referto.pdf", *me)
    docs.confirm(conn, confirmed, pid, *me)
    old = docs.ingest(conn, pid, _pdf("lettera del laboratorio, bozza"), "lettera.pdf", *me)
    docs.confirm(conn, old, pid, *me)
    # a corrected file keeps the old name: two rows called lettera.pdf is normal
    pending = docs.replace(conn, old, pid, _pdf("lettera del laboratorio, versione corretta"),
                           "lettera.pdf", *me)
    refused = docs.ingest(conn, pid, b"<svg xmlns='http://www.w3.org/2000/svg'/>", "foto.svg", *me)
    wrong = docs.ingest(conn, pid, b"appunti di un altro paziente", "appunti.txt", *me)
    docs.reject(conn, wrong, pid, "not this patient", *me)
    for did, status in ((confirmed, "confirmed"), (old, "superseded"), (pending, "pending_review"),
                        (refused, "quarantined"), (wrong, "rejected")):
        got = docs.row(conn, did)["status"]
        if got != status:
            raise RuntimeError(f"populated seed: document {did} is {got}, expected {status}")
    base = f"/patients/{CF}"
    return [
        ("patient-detail", base, ["Similar cases", "rct 26"]),
        ("documents", f"{base}/documents",
         ["confirmed", "pending review", "quarantined", "superseded", "rejected"]),
        ("documents-search", f"{base}/documents?q=otturazione", ["confirmed by", "otturazione dente 25"]),
        ("document-confirmed", f"{base}/documents/{confirmed}", ["Download the original", "composito"]),
        ("document-pending", f"{base}/documents/{pending}", ["Not part of the record yet", "Confirm",
                                                             "versione corretta"]),
        ("similar", f"{base}/visits/{src}/similar", ["no outcome recorded", "[…]", "Your verdict"]),
        ("similar-teaching", f"{base}/visits/{src}/similar?view=teaching",
         ["Teaching view", "no outcome recorded"]),
    ]


def start(tmp):
    """The disposable staff app. -> (server, pages)."""
    import logging
    import app.db as app_db
    import documents as docs
    from werkzeug.serving import make_server
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    from app import create_app
    from storage import init_db
    tmp = Path(tmp)
    app_db.DB_PATH = str(tmp / "clinic.sqlite")
    app_db.CHROMA_PATH = str(tmp / "chroma")
    docs.DOC_ROOT = tmp / "documents"
    docs.DOC_CHROMA_PATH = str(tmp / "doc_chroma")
    docs._collection_cache.clear()
    docs.SLOT_DIR = tmp / "slots"
    os.environ["CLINIC_SIMILAR_CASES"] = "1"
    app = create_app()
    conn = init_db(app_db.DB_PATH)
    try:
        pages = seed(conn)
    finally:
        conn.close()
    server = make_server("127.0.0.1", POPULATED_PORT, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, pages


def check(browser, widths, out_dir):
    """-> [(width, page, problem)], and the page-widths measured."""
    from a11y_audit import CONTRAST, FOCUS_TARGETS, SETTLE_MS, STYLE_OF
    problems, measured = [], 0
    with tempfile.TemporaryDirectory(prefix="populated-") as tmp:
        server, pages = start(tmp)
        try:
            for width in widths:
                ctx = browser.new_context(viewport={"width": width, "height": 900})
                page = ctx.new_page()
                page.goto(f"{POPULATED_URL}/login")
                page.fill('input[name="username"]', USER)
                page.fill('input[name="password"]', PASS)
                page.click('button[type="submit"]')
                page.wait_for_load_state("networkidle")
                if "/login" in page.url:
                    raise RuntimeError("populated: login failed")
                for name, path, must_show in pages:
                    page.goto(f"{POPULATED_URL}{path}")
                    page.wait_for_load_state("networkidle")
                    measured += 1
                    shot = Path(out_dir) / str(width) / f"populated-{name}.png"
                    shot.parent.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(shot), full_page=True)
                    body = page.inner_text("main")
                    for words in must_show:
                        if words not in body:
                            problems.append((width, name, f"empty: {words!r} not on the page"))
                    size = page.evaluate("() => [document.documentElement.scrollWidth, window.innerWidth]")
                    if size[0] > size[1] + 1:
                        problems.append((width, name, f"overflow +{size[0] - size[1]}px"))
                    for bad in page.evaluate(CONTRAST):
                        problems.append((width, name, f"contrast {bad['ratio']}:1 (needs {bad['need']})"
                                                      f" {bad['tag']} {bad['text']!r}"))
                    for bad in page.evaluate(NAMES):
                        problems.append((width, name, bad))
                    for target in page.evaluate(FOCUS_TARGETS):
                        # the collapsed menu at 390 is visibility:hidden - nothing in it
                        # can be reached, so there is no focus to see
                        if page.evaluate(HIDDEN, target["i"]):
                            continue
                        page.evaluate("() => document.activeElement && document.activeElement.blur()")
                        page.wait_for_timeout(SETTLE_MS)
                        before = page.evaluate(STYLE_OF, target["i"])
                        page.evaluate("(i) => document.querySelector('[data-a11y-i=\"' + i + '\"]').focus()",
                                      target["i"])
                        page.wait_for_timeout(SETTLE_MS)
                        if not page.evaluate(FOCUSED, target["i"]):
                            problems.append((width, name, f"cannot take focus: {target['tag']}"
                                                          f" {target['text']!r}"))
                        elif page.evaluate(STYLE_OF, target["i"]) == before:
                            problems.append((width, name, f"no focus indicator: {target['tag']}"
                                                          f" {target['text']!r}"))
                        elif not page.evaluate(CHROME_RING, target["i"]):
                            problems.append((width, name, f"not the design-system focus ring:"
                                                          f" {target['tag']} {target['text']!r}"))
                    print(f"  {width:>5}  populated  {name:<20} problems="
                          f"{sum(1 for p in problems if p[:2] == (width, name))}")
                ctx.close()
        finally:
            server.shutdown()
    return problems, measured


def main(argv):
    from playwright.sync_api import sync_playwright
    widths = [int(argv[i + 1]) for i, a in enumerate(argv) if a == "--width"] or [1440, 390]
    out = Path(argv[argv.index("--out") + 1]) if "--out" in argv else \
        Path(tempfile.gettempdir()) / "dental-shots"
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            problems, measured = check(browser, widths, out)
        finally:
            browser.close()
    for width, name, problem in problems:
        print(f"  {width}px  {name:<20} {problem}")
    print(f"{len(problems)} problem(s) over {measured} populated page-widths")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
