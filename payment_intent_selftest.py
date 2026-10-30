"""Manual payment retries, competing forms and recovery on disposable SQLite."""
import sqlite3
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import app.db as app_db
import clinic_time
import ledger
import patient_id
from app import payment_intent
from login_return_selftest import CF, PASSWORD, field, make_app


def connect():
    conn = sqlite3.connect(app_db.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def invoice_fixture(conn, suffix="one", cf=CF):
    pid = patient_id.resolve(conn, cf)
    vid = conn.execute(
        "INSERT INTO visits (patient_id, visit_date, procedures, clinical_notes, source_path)"
        " VALUES (?, '2026-10-28', '[]', '', ?)", (pid, "synthetic-payment-" + suffix)).lastrowid
    conn.execute("INSERT INTO invoices (patient_id, visit_id, line_index, amount, amount_cents, description)"
                 " VALUES (?, ?, 0, 1200, 120000, 'Synthetic payment test')", (pid, vid))
    inv = ledger.ensure_invoice(conn, pid, vid)
    conn.commit()
    ledger.issue(conn, inv, "return_dentist", "dentist")
    return inv


class PaymentIntentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="clinic-p29-payment-")
        self.old_paths = app_db.DB_PATH, app_db.CHROMA_PATH
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.restore_paths)
        self.app = make_app(self.tmp.name)
        self.conn = connect()
        self.addCleanup(self.conn.close)
        self.inv = invoice_fixture(self.conn)
        self.client = self.staff()

    def restore_paths(self):
        app_db.DB_PATH, app_db.CHROMA_PATH = self.old_paths

    def staff(self, name="return_dentist"):
        client = self.app.test_client()
        csrf = field(client.get("/login").text, "csrf_token")
        result = client.post("/login", data={"username": name, "password": PASSWORD, "csrf_token": csrf})
        self.assertEqual(result.status_code, 302)
        return client

    def start(self, client=None, inv=None):
        client, inv = client or self.client, inv or self.inv
        response = client.get(f"/billing/invoices/{inv}/payment/new")
        self.assertEqual(response.status_code, 303)
        url = response.location
        page = client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers.get("Cache-Control"), "no-store")
        self.assertEqual(page.headers.get("Referrer-Policy"), "no-referrer")
        self.assertEqual(set(parse_qs(urlsplit(url).query)), {"form"})
        self.assertTrue(parse_qs(urlsplit(url).query)["form"][0].isdigit())
        self.assertNotIn(field(page.text, "intent"), url)
        return url, {"intent": field(page.text, "intent"), "form_id": field(page.text, "form_id"),
                     "csrf_token": field(page.text, "csrf_token"),
                     "amount": "462.50", "method": "cash", "reference": "synthetic-receipt"}

    def post(self, data, client=None, inv=None):
        return (client or self.client).post(f"/billing/invoices/{inv or self.inv}/payment", data=data)

    def assert_payments(self, count, total):
        row = self.conn.execute("SELECT COUNT(*), COALESCE(SUM(amount_cents), 0) FROM payments").fetchone()
        self.assertEqual(tuple(row), (count, total))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM payment_allocations").fetchone()[0], count)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='record_payment'"
                                          " AND allowed=1").fetchone()[0], count)

    def test_back_reload_replay_and_new_explicit_payment(self):
        url, data = self.start()
        for _ in range(3):
            self.assertEqual(field(self.client.get(url).text, "intent"), data["intent"])
        for _ in range(3):
            response = self.post(data)
            self.assertEqual(response.status_code, 303)
            self.assertEqual(set(parse_qs(urlsplit(response.location).query)), {"receipt"})
            self.assertIn("Payment recorded", self.client.get(response.location).text)
            self.assertIn("Payment recorded", self.client.get(url).text)
            self.assertNotIn('name="amount"', self.client.get(url).text)
        self.assert_payments(1, 46250)
        _, another = self.start()
        self.assertNotEqual(another["intent"], data["intent"])
        self.post(another)
        self.assert_payments(2, 92500)
        self.assertEqual(ledger.summary(self.conn, self.inv)["outstanding_cents"], 27500)

    def test_two_preexisting_forms_only_one_can_commit(self):
        _, first = self.start()
        url, second = self.start()
        self.post(first)
        response = self.post(second)
        self.assertIn("payments have changed", self.client.get(response.location).text)
        self.assertNotIn('name="amount"', self.client.get(url).text)
        self.assert_payments(1, 46250)

    def test_changed_payload_cannot_reuse_a_consumed_key(self):
        _, data = self.start()
        self.post(data)
        for change in ({"amount": "10"}, {"method": "card"}, {"reference": "another"}, {"note": "changed"}):
            with self.subTest(change=change):
                response = self.post(dict(data, **change))
                self.assertIn("already used for different details", self.client.get(response.location).text)
                self.assert_payments(1, 46250)

    def test_tamper_missing_cross_invoice_and_cross_account_are_denied(self):
        _, data = self.start()
        other = invoice_fixture(self.conn, "two")
        assistant = self.staff("return_assistant")
        _, assistant_form = self.start(assistant)
        cases = [(dict(data, intent="invalid"), self.client, self.inv),
                 (dict(data, intent="", key="old-unbound-key"), self.client, self.inv),
                 (data, self.client, other),
                 (dict(data, csrf_token=assistant_form["csrf_token"]), assistant, self.inv)]
        for body, client, inv in cases:
            response = self.post(body, client, inv)
            self.assertEqual(response.status_code, 303)
            self.assertIn("Not done:", client.get(response.location).text)
        self.assert_payments(0, 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM audit_log WHERE reason='invalid_intent'")
                         .fetchone()[0], 4)
        self.post(assistant_form, assistant)
        self.assert_payments(1, 46250)
        self.assertEqual(self.conn.execute("SELECT recorded_role FROM payments").fetchone()[0], "assistant")

    def test_csrf_expired_session_and_admin_cannot_write(self):
        url, data = self.start()
        self.assertEqual(self.post(dict(data, csrf_token="invalid")).status_code, 400)
        admin = self.staff("return_admin")
        self.assertEqual(admin.get(url).status_code, 302)
        csrf = field(admin.get("/change-password").text, "csrf_token")
        self.assertEqual(self.post(dict(data, csrf_token=csrf), admin).status_code, 302)
        self.conn.execute("DELETE FROM sessions WHERE username='return_dentist'")
        self.conn.commit()
        response = self.post(data)
        self.assertTrue(response.location.endswith("/login"))
        self.assert_payments(0, 0)

    def test_expiry_and_clinic_midnight_keep_receipt_date_fixed(self):
        instant = datetime(2026, 10, 28, 23, 50, tzinfo=timezone.utc)
        with patch.object(clinic_time, "now_utc", return_value=instant):
            self.client = self.staff()
            url, data = self.start()
        with self.app.app_context():
            intent = payment_intent._signer().loads(data["intent"])
        self.assertEqual(intent["received_on"], "2026-10-29")
        with patch.object(clinic_time, "now_utc", return_value=instant + timedelta(minutes=10)):
            self.client = self.staff()
            data["csrf_token"] = field(self.client.get("/change-password").text, "csrf_token")
            self.post(data)
        with patch.object(clinic_time, "now_utc", return_value=instant + timedelta(minutes=40)):
            self.client = self.staff()
            data["csrf_token"] = field(self.client.get("/change-password").text, "csrf_token")
            self.conn.execute("DELETE FROM pending_actions")  # expired form metadata was swept
            self.conn.commit()
            result = self.post(data)  # exact receipt replay still succeeds after expiry/pruning
            self.assertIn("Payment recorded", self.client.get(result.location).text)
        self.assert_payments(1, 46250)
        self.assertEqual(self.conn.execute("SELECT received_on FROM payments").fetchone()[0], "2026-10-29")
        self.client = self.staff()
        _, fresh = self.start()
        with self.app.app_context():
            expires = clinic_time.read_instant(payment_intent._signer().loads(fresh["intent"])["expires"])
        with patch.object(clinic_time, "now_utc", return_value=expires + timedelta(seconds=1)):
            self.client = self.staff()
            fresh["csrf_token"] = field(self.client.get("/change-password").text, "csrf_token")
            result = self.post(fresh)
            self.assertIn("expired", self.client.get(result.location).text)
        self.assert_payments(1, 46250)

    def test_view_locators_are_not_write_authority_or_cross_account_access(self):
        url, data = self.start()
        assistant = self.staff("return_assistant")
        self.assertNotIn(data["intent"], assistant.get(url).text)
        self.assertNotIn('name="amount"', assistant.get(url).text)
        other = invoice_fixture(self.conn, "other-form")
        self.assertNotIn(data["intent"], self.client.get(url.replace(f"/{self.inv}/", f"/{other}/")).text)
        _, wrong_form = self.start()
        self.post(dict(data, form_id=wrong_form["form_id"]))
        self.assert_payments(0, 0)
        result = self.post(data)
        self.assertEqual(assistant.get(result.location).status_code, 404)
        self.assertEqual(self.client.get(result.location.replace(f"/{self.inv}/", f"/{other}/")).status_code, 404)
        self.assert_payments(1, 46250)

    def test_invalid_amount_retry_keeps_intent_and_does_not_consume_it(self):
        url, data = self.start()
        for value in ("", "-1", "0", "1200.01", "NaN", "Infinity", "1e9999", "nonsense"):
            response = self.post(dict(data, amount=value))
            self.assertEqual(response.status_code, 303, value)
            self.assertEqual(response.location, url)
            self.assertIn("Not done:", self.client.get(url).text)
            self.assert_payments(0, 0)
        self.post(dict(data, amount="462,50"))
        self.assert_payments(1, 46250)

    def test_atomic_audit_failure_rolls_back_and_retry_succeeds(self):
        _, data = self.start()
        self.conn.execute("CREATE TRIGGER test_audit_failure BEFORE INSERT ON audit_log"
                          " WHEN NEW.action='record_payment' AND NEW.allowed=1"
                          " BEGIN SELECT RAISE(ABORT, 'synthetic audit failure'); END")
        self.conn.commit()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "synthetic audit failure"):
            self.post(data)
        self.assert_payments(0, 0)
        self.conn.execute("DROP TRIGGER test_audit_failure")
        self.conn.commit()
        self.post(data)
        self.assert_payments(1, 46250)

    def test_lost_response_then_fresh_session_returns_existing_receipt(self):
        url, data = self.start()
        with patch("app.billing_routes.flash", side_effect=RuntimeError("synthetic lost response")):
            with self.assertRaisesRegex(RuntimeError, "lost response"):
                self.post(data)
        self.assert_payments(1, 46250)
        # Fresh SQLite connections and client; nothing relies on in-memory consumption.
        self.client = self.staff()
        self.assertIn("Payment recorded", self.client.get(url).text)
        data["csrf_token"] = field(self.client.get("/change-password").text, "csrf_token")
        self.post(data)
        self.assert_payments(1, 46250)

    def test_process_death_before_and_after_commit_then_retry(self):
        script = '''
import json, os, sqlite3, sys
import ledger
path, invoice, mode, key = sys.argv[1:]
class CrashConnection(sqlite3.Connection):
    def execute(self, sql, *args, **kwargs):
        if sql == "COMMIT" and mode == "before":
            os._exit(71)
        result = super().execute(sql, *args, **kwargs)
        if sql == "COMMIT" and mode == "after":
            os._exit(72)
        return result
conn = sqlite3.connect(path, factory=CrashConnection)
conn.row_factory = sqlite3.Row
result = ledger.record_payment(conn, int(invoice), "100", "cash", key,
                               "return_dentist", "dentist", expected_revision=0)
print(json.dumps(result))
conn.close()
'''
        for mode, exit_code in (("before", 71), ("after", 72)):
            inv = invoice_fixture(self.conn, "crash-" + mode)
            args = [sys.executable, "-c", script, app_db.DB_PATH, str(inv), mode, "crash-" + mode]
            failed = subprocess.run(args, capture_output=True, text=True, timeout=15)
            self.assertEqual(failed.returncode, exit_code, failed.stderr)
            self.assertEqual(ledger.summary(self.conn, inv)["paid_cents"], 0 if mode == "before" else 10000)
            args[-2] = "retry"
            retry = subprocess.run(args, capture_output=True, text=True, timeout=15)
            self.assertEqual(retry.returncode, 0, retry.stderr)
            self.assertEqual(json.loads(retry.stdout)[1], mode == "before")
            self.assertEqual(ledger.summary(self.conn, inv)["paid_cents"], 10000)
        self.assert_payments(2, 20000)

    def test_refund_stales_an_unused_form_and_keeps_original_receipt(self):
        original_url, data = self.start()
        self.post(data)
        url, stale = self.start()
        payment = self.conn.execute("SELECT id FROM payments").fetchone()[0]
        ledger.refund(self.conn, payment, "10", "synthetic-refund", "return_dentist", "dentist", "UAT")
        self.post(stale)
        self.assertIn("payments have changed", self.client.get(url).text)
        self.assertIn("Payment recorded", self.client.get(original_url).text)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM payments WHERE kind='payment'").fetchone()[0], 1)

    def test_void_and_patient_transfer_refuse_unconsumed_intent(self):
        _, data = self.start()
        ledger.void(self.conn, self.inv, "return_dentist", "dentist", "synthetic test")
        response = self.post(data)
        self.assertIn("no longer accepts payments", self.client.get(response.location).text)
        other = invoice_fixture(self.conn, "transfer")
        _, data = self.start(inv=other)
        other_pid = patient_id.seed_patient(self.conn, "ZZRT800101010102", "Synthetic Other", None)
        self.conn.execute("UPDATE billing_invoices SET patient_id=? WHERE id=?", (other_pid, other))
        self.conn.commit()
        response = self.post(data, inv=other)
        self.assertIn("different invoice or staff", self.client.get(response.location).text)
        self.assert_payments(0, 0)

    def test_concurrent_same_key_and_distinct_keys(self):
        for same_key in (True, False):
            inv = invoice_fixture(self.conn, f"race-{same_key}")
            revision = ledger.payment_revision(self.conn, inv)
            barrier = threading.Barrier(2)

            def pay(i):
                conn = connect()
                try:
                    barrier.wait(timeout=5)
                    key = f"race-{inv}-" + ("same" if same_key else str(i))
                    return ledger.record_payment(conn, inv, "100", "cash", key,
                                                 "return_dentist", "dentist", expected_revision=revision)
                except ledger.LedgerError as e:
                    return str(e)
                finally:
                    conn.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(pay, (1, 2)))
            self.assertEqual(sum(isinstance(r, tuple) and r[1] for r in results), 1, results)
            if same_key:
                self.assertEqual(results[0][0], results[1][0])
            else:
                self.assertEqual(sum(isinstance(r, str) and "payments have changed" in r for r in results), 1)
            self.assertEqual(ledger.summary(self.conn, inv)["paid_cents"], 10000)


if __name__ == "__main__":
    unittest.main(argv=[__file__], verbosity=2)
