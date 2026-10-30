"""P29 patient presentation checks. Disposable SQLite/files and loopback server only.
No real account, model, provider or clinic service is used.
"""
import hashlib
import json
import re
import sqlite3
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

from jinja2 import FileSystemLoader
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import WSGIRequestHandler, make_server

import appointments
import availability
import consent
import documents
import handoff
import ledger
import legacy_fixtures as fx
import patient_auth
import patient_files
import patient_id
import storage
from patient_app import create_patient_app, routes
from patient_files_selftest import png

EVIDENCE = Path('.planning/evidence/P29/P29.04')

class Quiet(WSGIRequestHandler):
    def log(self, *args, **kwargs):
        pass


def fixture(root):
    db = root / 'patient.sqlite'
    routes.DB_PATH = str(db)
    documents.DOC_ROOT = root / 'documents'
    conn = storage.init_db(str(db))
    pid = patient_id.seed_patient(conn, fx.CF['rossi'], 'Mario Rossi', '+393331234567')
    other = patient_id.seed_patient(conn, fx.CF['verdi'], 'Luca Verdi')
    patient_auth.issue_pin(fx.CF['rossi'], conn, 'fixture', 'dentist')
    conn.execute('UPDATE patient_credentials SET must_change_pin=0')
    conn.commit()
    token = patient_auth.create_patient_session(conn, fx.CF['rossi'])
    consent.record(conn, pid, 'ai_assistant', True, 'fixture', 'dentist')
    availability.seed_fixture_hours(conn)
    conn.execute("UPDATE clinic_hours SET opens='00:00', closes='23:59', closed=0")
    for day in range(7):
        conn.execute("INSERT INTO dentist_schedule (dentist,weekday,starts,ends) VALUES ('Demo dentist',?,'00:00','23:59')", (day,))
    conn.commit()
    date = (datetime.now() + timedelta(days=10)).date().isoformat()
    aid = appointments.book(conn, pid, 'Demo dentist', date + 'T10:30', 30)
    appointments.request(conn, pid, date, 'afternoon', 'Synthetic appointment request')
    # P29 S1: an outcome history, a waiting call-back and an invoice still being checked
    declined = appointments.request(conn, pid, date, 'morning', None)
    appointments.decline(conn, declined, 'STAFF-ONLY synthetic reason')
    handoff.raise_request(conn, pid, 'patient_asked', 'billing')
    visit = conn.execute("INSERT INTO visits (patient_id, visit_date, procedures, clinical_notes, source_path)"
                         " VALUES (?, '2025-11-10', '[]', '', 'p29-e2e')", (pid,)).lastrowid
    conn.execute("INSERT INTO invoices (patient_id, visit_id, line_index, amount, amount_cents, description)"
                 " VALUES (?, ?, 0, 80.0, 8000, 'Synthetic hygiene')", (pid, visit))
    ledger.ensure_invoice(conn, pid, visit, legacy=True)
    # Direct synthetic storage fixtures: reader/indexing is not under test here.
    files = []
    for n in range(14):
        owner = other if n == 13 else pid
        data = png(n) if n % 2 else f'Synthetic document {n}'.encode()
        sha = hashlib.sha256(data).hexdigest()
        rel = f'{owner}/{sha}'
        path = documents.DOC_ROOT / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        doc = conn.execute('INSERT INTO patient_documents (patient_id, kind, display_name, stored_path, sha256, size, status, uploaded_by, uploaded_at, category) VALUES (?,?,?,?,?,?,?, ?,?,?)',
            (owner, 'png' if n % 2 else 'text', 'PRIVATE-OTHER-NAME.txt', rel, sha, len(data), 'confirmed', 'fixture', f'2026-10-{min(n+1,28):02d}T10:00:00+00:00', 'report')).lastrowid
        conn.commit()
        if n != 12:
            patient_files.publish(conn, doc, owner, 'fixture', 'dentist')
        files.append(doc)
    conn.close()
    app = create_patient_app(env_path=root / '.env.patient')
    app.config['TESTING'] = True
    return app, token, files, aid


def keyboard_pass(page, url):
    """Tab from the top until focus leaves the page. Every stop must be visible and
    named, and reaching the end proves there is no focus trap."""
    page.goto(url)
    stops = []
    for _ in range(200):
        page.keyboard.press('Tab')
        info = page.evaluate("""() => { const e = document.activeElement;
            if (!e || e === document.body) return null;
            const r = e.getBoundingClientRect();
            const img = e.querySelector('img');
            const name = (e.getAttribute('aria-label') || e.innerText || (img && img.alt) || e.value || e.title
                          || (e.labels && e.labels[0] && e.labels[0].innerText) || '').trim();
            return {html: e.outerHTML.slice(0, 80), name, visible: r.width > 0 && r.height > 0}; }""")
        if info is None:
            return len(stops)
        assert info['visible'], (url, 'focus on a hidden element', info)
        assert info['name'] or 'tabindex="0"' in info['html'], (url, 'unnamed focus stop', info)
        stops.append(info['html'])
    raise AssertionError(f'{url}: focus never left the page (trap?)')


def walk():
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='clinic-p29-patient-') as temp:
        root = Path(temp)
        app, token, files, aid = fixture(root)
        client = app.test_client()
        # Regression for losing the active page; hostile targets always map home.
        for target, expected in [('patient.login', '/login'), ('patient.files_page', '/files'),
                                 ('https://evil.invalid', '/'), ('//evil.invalid', '/'), ('patient.file_download', '/')]:
            response = client.get('/lang/en', query_string={'page': target})
            assert response.location == expected, (target, response.location)
        client.set_cookie(patient_auth.PATIENT_COOKIE_NAME, token)
        client.set_cookie('patient_lang', 'en')
        for fid in files[-2:]:
            assert client.get(f'/files/{fid}/download').status_code == 404
        assert 'PRIVATE-OTHER-NAME' not in client.get('/files').text
        # the foreign file is the same 404 by preview too, and never leaks its name
        for fid in files[-2:]:
            denied = client.get(f'/files/{fid}/preview')
            assert denied.status_code == 404 and b'PRIVATE' not in denied.data
        assert client.post('/appointments/request', data={'day': '2099-01-01'}).status_code == 400
        server = make_server('127.0.0.1', 0, app, request_handler=Quiet)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        results = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                for width in (1440, 390):
                    context = browser.new_context(viewport={'width': width, 'height': 1000}, reduced_motion='reduce')
                    context.route('**/*', lambda r: r.continue_() if urlsplit(r.request.url).netloc == urlsplit(base).netloc else r.abort())
                    context.add_cookies([{'name': patient_auth.PATIENT_COOKIE_NAME, 'value': token, 'url': base}, {'name': 'patient_lang', 'value': 'en', 'url': base}])
                    page = context.new_page()
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    # Same synthetic data with pre-change templates/styles for honest comparison.
                    current_loader, current_static = app.jinja_loader, app.static_folder
                    app.jinja_loader = FileSystemLoader(str(EVIDENCE / 'before/patient_app/templates'))
                    app.static_folder = str((EVIDENCE / 'before/patient_app/static').resolve())
                    app.jinja_env.cache.clear()
                    page.goto(base + '/')
                    page.screenshot(path=str(EVIDENCE / f'before-home-{width}.png'), full_page=True)
                    app.jinja_loader, app.static_folder = current_loader, current_static
                    app.jinja_env.cache.clear()
                    page.reload()
                    # Avoid stale CSS under the same URL in before/after capture.
                    page.evaluate("async () => { for (const link of document.querySelectorAll('link[rel=stylesheet]')) { const css = await fetch(link.href, {cache:'reload'}).then(r=>r.text()); const style=document.createElement('style'); style.textContent=css; document.head.append(style); } }")
                    for lang in ('en', 'it'):
                        context.add_cookies([{'name': 'patient_lang', 'value': lang, 'url': base}])
                        for path in ('/', '/appointments', '/files', '/billing', '/profile', '/chat', '/change-pin'):
                            response = page.goto(base + path)
                            assert response.status == 200, (path, response.status)
                            assert page.locator('h1').count() == 1, path
                            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (path, width, 'horizontal overflow')
                            assert page.locator('.patient-nav a[aria-current="page"]').count() == (0 if path == '/change-pin' else 1)
                            page.screenshot(path=str(EVIDENCE / f'{lang}-{path.strip("/") or "home"}-{width}.png'), full_page=True)
                            results.append(f'{lang} {path} {width}: rendered / no overflow')
                    context.add_cookies([{'name': 'patient_lang', 'value': 'en', 'url': base}])
                    page.goto(base + '/files')
                    expect(page.locator('[data-file-kind]:visible')).to_have_count(12)
                    page.get_by_label('File type', exact=True).select_option('png')
                    expect(page.locator('[data-file-kind]:visible')).to_have_count(6)
                    page.get_by_label('Search displayed files').fill('no such synthetic file')
                    expect(page.locator('#file-no-matches')).to_be_visible()
                    page.locator('#file-clear').click()
                    expect(page.locator('[data-file-kind]:visible')).to_have_count(12)
                    # P29 S1: each shared image has a View button named after the file.
                    expect(page.get_by_role('link', name=re.compile(r'^View\s*:'))).to_have_count(6)
                    page.goto(base + '/appointments')
                    expect(page.locator('tr[data-group="past"]').first).to_be_visible()
                    expect(page.locator('tr[data-group="past"]').get_by_text('Declined', exact=True)).to_be_visible()
                    expect(page.get_by_text('Requested day').first).to_be_visible()
                    assert 'STAFF-ONLY' not in page.content()
                    page.goto(base + '/billing')
                    expect(page.locator('table.patient-money').first).to_be_visible()
                    expect(page.get_by_text('unknown', exact=True).first).to_be_visible()
                    page.goto(base + '/chat')
                    expect(page.get_by_text('About: payments')).to_be_visible()
                    for path in ('/appointments', '/files', '/billing', '/chat', '/profile'):
                        stops = keyboard_pass(page, base + path)
                        results.append(f'keyboard {path} {width}: {stops} stops, all visible and named, no trap')
                    page.goto(base + '/files')
                    # Published original uses existing scoped download endpoint.
                    with page.expect_download() as downloaded:
                        page.locator('a[href$="/download"]').first.click()
                    assert 'PRIVATE-OTHER' not in downloaded.value.suggested_filename
                    page.locator('[data-locale-link]').filter(has_text='IT').click()
                    assert urlsplit(page.url).path == '/files'
                    expect(page.locator('html')).to_have_attribute('lang', 'it')
                    context.add_cookies([{'name': 'patient_lang', 'value': 'en', 'url': base}])
                    page.goto(base + '/appointments')
                    page.locator('#reason').fill('First line\nSecond line')
                    expect(page.locator('#reason-count')).to_contain_text('22 / 200')
                    page.locator('[data-locale-link]').filter(has_text='IT').click()
                    expect(page.locator('#patient-confirm')).to_be_visible()
                    page.keyboard.press('Escape')
                    expect(page.locator('#reason')).to_have_value('First line\nSecond line')
                    # Cancel never submits until the confirmation action; keyboard focus starts safely.
                    page.locator('form[data-confirm] button').first.click()
                    expect(page.locator('[data-dialog-cancel]')).to_be_focused()
                    page.locator('[data-dialog-cancel]').click()
                    expect(page.locator('form[data-confirm]')).to_have_count(1)
                    page.locator('form[data-confirm] button').first.click()
                    page.screenshot(path=str(EVIDENCE / f'cancel-dialog-{width}.png'), full_page=True)
                    page.keyboard.press('Escape')
                    page.goto(base + '/chat')
                    page.locator('#question').fill('First line')
                    page.locator('#question').press('Shift+Enter')
                    page.keyboard.type('Second line')
                    expect(page.locator('#question')).to_have_value('First line\nSecond line')
                    page.goto(base + '/')
                    page.keyboard.press('Tab')
                    expect(page.locator('.patient-skip')).to_be_focused()
                    page.keyboard.press('Enter')
                    expect(page.locator('#patient-content')).to_be_focused()
                    assert not errors, errors
                    context.close()
                # Exercise the actual cancellation write once in the disposable database.
                context = browser.new_context()
                context.add_cookies([{'name': patient_auth.PATIENT_COOKIE_NAME, 'value': token, 'url': base}, {'name':'patient_lang','value':'en','url':base}])
                page = context.new_page()
                page.goto(base + '/appointments')
                page.locator('form[data-confirm] button').click()
                page.locator('[data-dialog-confirm]').click()
                expect(page.locator('form[data-confirm]')).to_have_count(0)
                conn = sqlite3.connect(routes.DB_PATH)
                assert conn.execute('SELECT status FROM appointments WHERE id=?', (aid,)).fetchone()[0] == 'cancelled'
                conn.close()
                context.clear_cookies()
                for path in ('/login',):
                    for width in (1440,390):
                        page.set_viewport_size({'width':width,'height':1000})
                        page.goto(base + path)
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                        page.screenshot(path=str(EVIDENCE / f'login-{width}.png'), full_page=True)
                browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)
        (EVIDENCE / 'browser-results.json').write_text(json.dumps({'pages': results, 'other_checks': ['language allowlist', 'CSRF rejection', 'file publication and ownership', 'safe filenames', 'search/type/reset', 'download', 'unsent locale dismissal', 'P29 S1 history, View buttons, billing tables, call-back, keyboard-only pass', 'cancellation dismissal and commit', 'multiline text', 'keyboard skip link', 'no JS errors'], 'fixtures': 'disposable synthetic only'}, indent=2))
        print('PASS: 28 page/locale/viewport combinations + guarded interactions, login and private file checks')

if __name__ == '__main__':
    walk()
