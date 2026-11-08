"""The owner's rules of 2026-11-08 on the patient portal, as e2e_ui_rules.py checks them on the staff app: one size
of button (48 px here, the portal's size for a finger), nothing that looks like a text link, and lists that keep their size and scroll inside their box. One
throwaway patient with visits, invoices, appointments and data requests signs in with a PIN and every portal page is
checked in Chromium at 1280 and 390. usage: e2e_portal_ui_rules.py [--headed]"""
import json
import os
import sys
import tempfile
import threading
from datetime import date, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright
from werkzeug.serving import WSGIRequestHandler, make_server

CF = "ZZPU800101010101"
PIN = "86420135"
PAGES = ("/", "/appointments", "/billing", "/files", "/chat", "/profile")

AUDIT = r"""() => {
  // the page itself; the header's pill menu and language switch are the portal's frame, as the staff menu is
  const main = document.querySelector('#patient-content');
  main.querySelectorAll('details:not([open])').forEach((d) => { d.open = true; });
  const shown = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const words = (el) => (el.getAttribute('aria-label') || el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 40);
  const out = {links: [], buttons: []};
  // tabs, tiles, the file viewer's own controls and the brand are their own controls
  const control = '.btn, .patient-tab, .pp-link-card, .pp-vbtn, .pp-v-arrow, .pp-file-thumb, .patient-avatar-link, .patient-brand-inline, .patient-skip';
  main.querySelectorAll('a[href]').forEach((a) => {
    if (!shown(a) || a.matches(control)) return;
    const s = getComputedStyle(a);
    if (!(parseFloat(s.borderTopWidth) >= 1 && parseFloat(s.paddingLeft) >= 6 && !s.textDecorationLine.includes('underline'))) out.links.push(words(a));
  });
  main.querySelectorAll('.btn-link').forEach((b) => { if (shown(b)) out.links.push('btn-link: ' + words(b)); });
  main.querySelectorAll('.btn, details > summary').forEach((b) => {
    if (!shown(b)) return;
    const h = Math.round(b.getBoundingClientRect().height);
    if (h < 46 || h > 50) out.buttons.push(words(b) + ' ' + h + 'px');   // the portal's one size: 48 px, for a finger
  });
  return out;
}"""

GROW = r"""(i) => {
  const main = document.querySelector('#patient-content');
  const lists = [...main.querySelectorAll('table, .pp-todo, .pp-files, .pp-consents')]
    .filter((el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
  if (i >= lists.length) return null;
  const list = lists[i];
  const body = list.tagName === 'TABLE' ? list.tBodies[0] : list;
  const row = body && body.firstElementChild;
  if (!row) return {name: '', bounded: true};
  const added = [];
  for (let k = 0; k < 60; k++) { const c = row.cloneNode(true); c.removeAttribute('id'); body.appendChild(c); added.push(c); }
  let bounded = false;
  for (let n = list; n && n !== main; n = n.parentElement) {
    const s = getComputedStyle(n);
    if (['auto', 'scroll'].includes(s.overflowY) && n.scrollHeight > n.clientHeight + 2) { bounded = true; break; }
  }
  added.forEach((c) => c.remove());
  return {name: (list.id || list.className || list.tagName).toString().slice(0, 50), bounded};
}"""


class Quiet(WSGIRequestHandler):
    def log(self, *a, **k):
        pass


def main():
    headed = "--headed" in sys.argv
    problems = []
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["PATIENT_COOKIE_SECURE"] = "0"
        import patient_auth
        import patient_id
        import storage
        from patient_app import create_patient_app, routes
        routes.DB_PATH = str(Path(tmp) / "clinic.sqlite")
        conn = storage.init_db(routes.DB_PATH)
        pid = patient_id.seed_patient(conn, CF, "Paola Ultima", "3330002222")
        today = date.today()
        for k in range(4):
            vid = conn.execute("INSERT INTO visits (patient_id, visit_date, procedures, clinical_notes, source_path)"
                               " VALUES (?, ?, ?, 'ok', ?)", (pid, (today - timedelta(days=30 * (k + 1))).isoformat(),
                                                             json.dumps(["prophy"]), f"sorted/{pid}/v{k}.json")).lastrowid
            conn.execute("INSERT INTO invoices (patient_id, visit_id, line_index, amount, description) VALUES (?, ?, 0, 60, 'pulizia')",
                         (pid, vid))
        import clinic_time
        now = clinic_time.stamp()
        conn.execute("INSERT INTO appointments (patient_id, dentist, starts_at, minutes, status, note, period, created_at,"
                     " updated_at) VALUES (?, '', ?, 0, 'requested', 'pulizia', 'afternoon', ?, ?)",
                     (pid, f"{today + timedelta(days=9)}T00:00:00", now, now))
        conn.execute("INSERT INTO data_requests (patient_id, kind, requested_by, requested_role, requested_at)"
                     " VALUES (?, 'export', ?, 'patient', ?)", (pid, pid, now))
        conn.commit()
        pin = patient_auth.issue_pin(CF, conn, "dentist", "dentist")
        app = create_patient_app()
        server = make_server("127.0.0.1", 0, app, request_handler=Quiet, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=not headed)
                first = True
                for width in (1280, 390):
                    ctx = browser.new_context(viewport={"width": width, "height": 900})
                    page = ctx.new_page()
                    page.goto(base + "/login")
                    page.fill("#codice_fiscale", CF)
                    page.fill("#pin", pin if first else PIN)
                    with page.expect_navigation():
                        page.click("form[action*='login'] button[type=submit]")
                    if first:
                        page.fill("#pin", PIN)
                        page.fill("#confirm", PIN)
                        with page.expect_navigation():
                            page.click("form[action*='change-pin'] button[type=submit]")
                        first = False
                    for path in PAGES:
                        page.goto(base + path)
                        page.wait_for_timeout(300)
                        where = f"{width} {path}"
                        for kind, items in page.evaluate(AUDIT).items():
                            problems += [f"{where}: {kind}: {item}" for item in items]
                        i = 0
                        while (got := page.evaluate(GROW, i)) is not None:
                            if not got["bounded"]:
                                problems.append(f"{where}: list grows the page: {got['name']}")
                            i += 1
                        wide = page.evaluate("document.documentElement.scrollWidth")
                        if wide > width + 1:
                            problems.append(f"{where}: the page scrolls sideways ({wide}px)")
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
    print(f"PASS portal: {len(PAGES)} pages at 1280 and 390 - one button size, no text links, lists keep their size")


if __name__ == "__main__":
    main()
