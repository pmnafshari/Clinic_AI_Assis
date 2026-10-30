"""Authentication return paths against fresh synthetic storage and real CSRF."""
import html
import re
import secrets
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from werkzeug.security import generate_password_hash

import app as staff_app
import app.db as app_db
import patient_id
import clinic_time
import web_session

CF = "ZZRT800101010101"
PASSWORD = "synthetic-return-password"
TARGET = f"/patients/{CF}/documents?page=2#page-2"


def field(page, name):
    match = re.search(r'name="' + name + r'" value="([^"]*)"', page)
    assert match, f"Missing form field: {name}"
    return html.unescape(match.group(1))


def make_app(root):
    root = Path(root).resolve()
    assert root.is_dir() and not (root / "clinic.sqlite").exists()
    app_db.DB_PATH = str(root / "clinic.sqlite")
    app_db.CHROMA_PATH = str(root / "chroma")
    with patch.object(staff_app, "load_secret_key", return_value=secrets.token_hex(32)):
        app = staff_app.create_app()
    app.config["TESTING"] = True
    with sqlite3.connect(app_db.DB_PATH) as conn:
        patient_id.seed_patient(conn, CF, "Synthetic Return", None)
        for name, role, forced in (("return_dentist", "dentist", 0),
                                   ("return_assistant", "assistant", 0),
                                   ("return_admin", "admin", 0),
                                   ("return_new", "dentist", 1)):
            conn.execute("INSERT INTO users (username, password_hash, role, active, must_change_password)"
                         " VALUES (?, ?, ?, 1, ?)",
                         (name, generate_password_hash(PASSWORD), role, forced))
    return app


class LoginReturnTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="clinic-p29-login-")
        self.old_db, self.old_index = app_db.DB_PATH, app_db.CHROMA_PATH
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.restore_paths)
        self.app = make_app(self.tmp.name)
        self.client = self.app.test_client()

    def restore_paths(self):
        app_db.DB_PATH, app_db.CHROMA_PATH = self.old_db, self.old_index

    def login(self, client, page, user="return_dentist", password=PASSWORD, **extra):
        data = {"username": user, "password": password,
                "csrf_token": field(page, "csrf_token"), "next": field(page, "next")}
        data.update(extra)
        return client.post("/login", data=data)

    def test_local_destination_survives_failed_login_and_query(self):
        redirect = self.client.get(TARGET.split("#")[0])
        self.assertEqual(redirect.status_code, 302)
        self.assertEqual(parse_qs(urlsplit(redirect.location).query).get("next"),
                         [TARGET.split("#")[0]], "protected GET lost its destination")
        page = self.client.get(redirect.location).text
        failed = self.login(self.client, page, password="wrong")
        self.assertIn("invalid username or password", failed.text)
        self.assertEqual(field(failed.text, "next"), TARGET.split("#")[0])
        result = self.login(self.client, failed.text, next=TARGET)
        self.assertEqual(result.location, TARGET)
        self.assertEqual(self.client.get(result.location).status_code, 200)

    def test_untrusted_destinations_do_not_redirect_or_skip_csrf(self):
        attacks = ["https://example.invalid/", "//example.invalid/", "/\\example.invalid/",
                   "/%2fexample.invalid/", "/%252fexample.invalid/", "/%255cexample.invalid/",
                   "/patients%0d%0aLocation:evil", "/patients?x=%250aevil",
                   "/patients#%00evil", "/../patients", "/login?next=/patients",
                   "/logout", "/change-password", "/api/session", "/static/js/app.js",
                   "/billing/invoices/1/payment", "/unknown", " /patients", "/" + "a"*2100]
        for target in attacks:
            with self.subTest(target=target[:50]):
                client = self.app.test_client()
                page = client.get("/login", query_string={"next": target}).text
                self.assertEqual(field(page, "next"), "")
                result = self.login(client, page, next=target)
                self.assertEqual(result.location, "/")
        client = self.app.test_client()
        client.get("/login")
        self.assertEqual(client.post("/login", data={"username": "return_dentist",
                         "password": PASSWORD, "next": TARGET}).status_code, 400)

    def test_role_is_checked_after_login(self):
        for role in ("assistant", "admin"):
            client = self.app.test_client()
            page = client.get("/login", query_string={"next": TARGET}).text
            result = self.login(client, page, user=f"return_{role}")
            self.assertEqual(result.location, TARGET)
            denied = client.get(result.location)
            self.assertEqual(denied.location, "/")
        with sqlite3.connect(app_db.DB_PATH) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM audit_log WHERE"
                             " action='document_read' AND allowed=0").fetchone()[0], 2)

    def test_forced_password_change_keeps_target_and_revokes_session(self):
        page = self.client.get("/login", query_string={"next": TARGET}).text
        signed = self.login(self.client, page, user="return_new")
        gate = self.client.get(signed.location)
        page = self.client.get(gate.location).text
        # Browsers inherit fragments across redirects; the explicit hidden field carries it on POST.
        data = {"current": PASSWORD, "password": "new-synthetic-password",
                "confirm": "mismatch", "csrf_token": field(page, "csrf_token"), "next": TARGET}
        failed = self.client.post("/change-password", data=data)
        self.assertEqual(field(failed.text, "next"), TARGET)
        data["confirm"] = data["password"]
        changed = self.client.post("/change-password", data=data)
        self.assertEqual(parse_qs(urlsplit(changed.location).query)["next"], [TARGET])
        with sqlite3.connect(app_db.DB_PATH) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sessions WHERE username='return_new'")
                             .fetchone()[0], 0)
        page = self.client.get(changed.location).text
        result = self.login(self.client, page, user="return_new", password=data["password"])
        self.assertEqual(result.location, TARGET)
        self.assertEqual(self.client.get(result.location).status_code, 200)

    def test_expired_write_is_not_a_return_destination(self):
        page = self.client.get("/login").text
        self.login(self.client, page)
        with sqlite3.connect(app_db.DB_PATH) as conn:
            conn.execute("UPDATE sessions SET last_seen_at=?", ((clinic_time.now_utc()-timedelta(hours=2)).isoformat(),))
            before = conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
        result = self.client.post("/billing/invoices/1/payment", data={
            "csrf_token": field(page, "csrf_token"), "amount": "100", "key": "expired"})
        self.assertEqual(result.location, "/login")
        with sqlite3.connect(app_db.DB_PATH) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0], before)
        page = self.client.get(result.location).text
        self.assertEqual(field(page, "next"), "")

    def test_generic_failures_lockout_and_independent_targets(self):
        first = self.client.get("/login", query_string={"next": TARGET}).text
        second = self.client.get("/login", query_string={"next": "/stock"}).text
        unknown = self.login(self.client, first, user="does_not_exist")
        wrong = self.login(self.client, first, password="wrong")
        self.assertIn("invalid username or password", unknown.text)
        self.assertIn("invalid username or password", wrong.text)
        for _ in range(4):
            self.login(self.client, first, password="wrong")
        locked = self.login(self.client, second)
        self.assertEqual(locked.status_code, 200)
        self.assertIn("invalid username or password", locked.text)
        self.assertEqual(field(locked.text, "next"), "/stock")
        # Another role in this tab keeps that tab's target, not the last opened form's target.
        result = self.login(self.client, first, user="return_assistant")
        self.assertEqual(result.location, TARGET)

    def test_access_changes_between_login_and_destination_are_enforced(self):
        page = self.client.get("/login", query_string={"next": TARGET}).text
        signed = self.login(self.client, page)
        with sqlite3.connect(app_db.DB_PATH) as conn:
            conn.execute("UPDATE users SET role='assistant' WHERE username='return_dentist'")
        self.assertEqual(self.client.get(signed.location).location, "/")
        with sqlite3.connect(app_db.DB_PATH) as conn:
            conn.execute("UPDATE users SET active=0 WHERE username='return_dentist'")
        denied = self.client.get(signed.location)
        self.assertEqual(urlsplit(denied.location).path, "/login")

    def test_query_and_anchor_are_html_escaped_and_known_get_pages_only(self):
        for target in ("/guides/sources/3?page=7#page-7", "/appointments?view=month&day=2026-10-28",
                       TARGET + '\" onfocus=\"alert(1)'):
            page = self.client.get("/login", query_string={"next": target}).text
            if ' ' in target:
                self.assertEqual(field(page, "next"), "")
            else:
                self.assertEqual(field(page, "next"), target)
        target = '/patients?search=%22%3E%3Cscript%3E#%22'
        page = self.client.get("/login", query_string={"next": target}).text
        self.assertEqual(field(page, "next"), target)
        self.assertNotIn('<script>#', page)


if __name__ == "__main__":
    unittest.main(argv=[__file__], verbosity=2)
