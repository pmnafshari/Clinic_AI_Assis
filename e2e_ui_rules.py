"""Owner rules for every staff page (2026-11-08), checked in a real browser:

1. one size of button - every button on a page is the same height;
2. nothing looks like a text link - whatever takes you somewhere or does something is a button;
3. a list keeps its size - a table or a list of rows that gets longer scrolls inside its box instead of growing the
   page, and a long choice list (a select with many options) opens as a list of fixed height that scrolls.

Every page each role can open is visited on a throwaway clinic (the rbac_selftest fixtures, plus a note the dental
chart suggests from, so its popup is checked too). A list is tested by what it does: sixty more rows are put in it
and the page must not grow. Chromium at 1280. usage: e2e_ui_rules.py [--headed] [--only ENDPOINT]
"""
import json
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright
from werkzeug.serving import WSGIRequestHandler, make_server

import auth
import rbac_selftest as rb
import web_session

# pages that are not pages: files, fragments for htmx, device and json endpoints, the language switch
SKIP = ("static", "shared", "set_language", "auth.logout", "auth.session", "billing.export_csv", "reports.mine_csv",
        "data_requests.download", "documents.original", "documents.raw", "documents.preview", "documents.text",
        "imports.original", "guides.page_image", "patients.search_fragment", "patients.files_fragment",
        "patients.edit_form_fragment", "patients.visit_edit_form_fragment", "upload.recent_intake",
        "admin.confirm_fragment", "imaging.files", "jarvis.handoff")

AUDIT = r"""() => {
  const main = document.querySelector('main#main');
  // every tab and every folded part of the page is checked, not only what shows first
  document.querySelectorAll('[role=tabpanel][hidden]').forEach((p) => { p.hidden = false; });
  main.querySelectorAll('details:not([open])').forEach((d) => { d.open = true; });
  const shown = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const scope = [main, ...document.querySelectorAll('.modal.show')];
  const all = (sel) => scope.flatMap((root) => [...root.querySelectorAll(sel)]).filter((el, i, arr) => arr.indexOf(el) === i);
  const words = (el) => (el.getAttribute('aria-label') || el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 40);
  const out = {links: [], buttons: [], selects: [], menus: []};
  // 1. a link is a button: boxed, padded, not underlined. tabs, tiles and list rows are their own controls
  const control = '.btn, .ux-tab, .sx-th, .appt-cal-day, .ux-wk-head, .ux-wk-appt, .list-group-item, .ux-kpi, .ux-quick-item, .file-thumb-link, .sx-tabs a, [role=tab], .dropdown-item';
  all('a[href]').forEach((a) => {
    if (!shown(a) || a.matches(control)) return;
    const s = getComputedStyle(a);
    const boxed = parseFloat(s.borderTopWidth) >= 1 && parseFloat(s.paddingLeft) >= 6 && !s.textDecorationLine.includes('underline');
    if (!boxed) out.links.push(words(a));
  });
  all('.btn-link').forEach((b) => { if (shown(b)) out.links.push('btn-link: ' + words(b)); });
  // 2. one height for every button, and every link drawn as one
  all('.btn, main#main details:not(.ux-panel) > summary, a[href]:not(.sx-th):not(.appt-cal-day):not(.ux-wk-head):not(.ux-wk-appt):not(.list-group-item):not(.ux-kpi):not(.ux-quick-item):not(.file-thumb-link):not([role=tab]):not(.sx-tabs a):not(.dropdown-item)').forEach((b) => {
    if (!shown(b) || b.closest('.input-group-pw')) return;
    const h = Math.round(b.getBoundingClientRect().height);
    if (h < 36 || h > 40) out.buttons.push(words(b) + ' ' + h + 'px');
  });
  // 3a. a long choice list must not open the full height of the screen
  all('select').forEach((s) => {
    if (shown(s) && s.options.length > 12 && !s.hasAttribute('data-pick-ready')) out.selects.push((s.name || s.id) + ' ' + s.options.length);
  });
  document.querySelectorAll('.dropdown-menu').forEach((m) => {
    const s = getComputedStyle(m);
    if (s.maxHeight === 'none' || !['auto', 'scroll'].includes(s.overflowY)) out.menus.push(m.className.slice(0, 50));
  });
  return out;
}"""

GROW = r"""(i) => {
  const main = document.querySelector('main#main');
  const lists = [...main.querySelectorAll('table, .ds-rows, .list-group, ol.tl, ol.timeline, .ux-requests')]
    .filter((el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
  if (i >= lists.length) return null;
  const list = lists[i];
  const body = list.tagName === 'TABLE' ? list.tBodies[0] : list;
  const row = body && body.firstElementChild;
  if (!row) return {name: '', grew: 0, bounded: true};
  const before = document.documentElement.scrollHeight;
  const added = [];
  for (let k = 0; k < 60; k++) { const c = row.cloneNode(true); c.removeAttribute('id'); c.hidden = false; body.appendChild(c); added.push(c); }
  const grew = document.documentElement.scrollHeight - before;
  // it keeps its size when a box around it now scrolls instead of the page growing
  let bounded = false;
  for (let n = list; n && n !== main; n = n.parentElement) {
    const s = getComputedStyle(n);
    if (['auto', 'scroll'].includes(s.overflowY) && n.scrollHeight > n.clientHeight + 2) { bounded = true; break; }
  }
  added.forEach((c) => c.remove());
  const name = (list.id || list.getAttribute('aria-labelledby') || list.className || list.tagName).toString().slice(0, 50);
  return {name, grew, bounded};
}"""


class Quiet(WSGIRequestHandler):
    def log(self, *a, **k):
        pass


def _url(rule, args):
    url = rule.rule
    for name in rule.arguments:
        for form in (f"<int:{name}>", f"<path:{name}>", f"<{name}>"):
            url = url.replace(form, str(args.get(name, "")))
    return url


def main():
    headed = "--headed" in sys.argv
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
    problems = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db_path = str(root / "clinic.sqlite")
        rb.app_db.DB_PATH = db_path
        rb.app_db.CHROMA_PATH = str(root / "chroma")
        import documents
        documents.DOC_ROOT = root / "documents"
        documents.DOC_CHROMA_PATH = str(root / "doc_chroma")
        documents._collection_cache.clear()
        import clinic_guides
        clinic_guides.DB_PATH = str(root / "guides.sqlite")
        clinic_guides.STORE = root / "guides"
        for mod in (rb.agent_routes, rb.notes_routes, rb.patients_routes, rb.upload_routes):
            mod.SORTED_ROOT = root / "sorted"
        rb.upload_routes.DROP_DIR = root / "drop"
        for mod in (rb.agent_routes, rb.notes_routes, rb.qa_routes):
            mod._urlopen = rb._no_model
        app = rb.create_app()
        args = rb._seed(db_path, root)
        conn = sqlite3.connect(db_path)
        pid = conn.execute("SELECT patient_id FROM patients WHERE codice_fiscale = ?", (rb.CANARY_CF,)).fetchone()[0]
        conn.execute("INSERT INTO visits (patient_id, visit_date, procedures, clinical_notes, source_path)"
                     " VALUES (?, '2026-02-02', ?, 'ok', 'ui.json')", (pid, json.dumps(["comp 36 MO", "ext 48"])))
        conn.commit()
        server = make_server("127.0.0.1", 0, app, request_handler=Quiet, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        rules = sorted((r for r in app.url_map.iter_rules() if "GET" in r.methods and r.endpoint not in SKIP
                        and not r.endpoint.startswith("jarvis.api_")), key=lambda r: r.endpoint)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=not headed)
                for role in rb.ROLES:
                    token = web_session.create_session(conn, f"rb_{role}", role)
                    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
                    ctx.add_cookies([{"name": web_session.COOKIE_NAME, "value": token, "url": base}])
                    page = ctx.new_page()
                    seen = 0
                    for rule in rules:
                        need = auth.ROUTE_POLICY.get(rule.endpoint)
                        if need not in ("login", "public") and not auth.authorize(role, need):
                            continue
                        if only and rule.endpoint != only:
                            continue
                        resp = page.goto(base + _url(rule, args))
                        if resp is None or "text/html" not in resp.headers.get("content-type", "") \
                                or page.locator("main#main").count() == 0:
                            continue
                        page.wait_for_timeout(350)   # the popup's fade, the list tools
                        where = f"{role} {page.url.replace(base, '')}"
                        found = page.evaluate(AUDIT)
                        for kind, items in found.items():
                            for item in items:
                                problems.append(f"{where}: {kind}: {item}")
                        i = 0
                        while True:
                            got = page.evaluate(GROW, i)
                            if got is None:
                                break
                            if not got["bounded"]:
                                problems.append(f"{where}: list grows the page by {got['grew']}px: {got['name']}")
                            i += 1
                        seen += 1
                    print(f"{role}: {seen} pages")
                    ctx.close()
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            conn.close()
    for line in sorted(set(problems)):
        print("FAIL", line)
    if problems:
        sys.exit(1)
    print("PASS every page: one button size, no text links, lists keep their size")


if __name__ == "__main__":
    main()
