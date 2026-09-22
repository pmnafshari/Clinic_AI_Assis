"""Billing through the real routes (P07): the staff workflow, the portal, and
one amount shown the same way everywhere it appears."""
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

from werkzeug.security import generate_password_hash

import app.db as app_db
import ask
import ledger
import patient_auth
import patient_id
import web_session
from app import create_app
from patient_app import chat, create_patient_app, routes as patient_routes

CF = "ZZBL800101010101"
OTHER = "ZZBL800101010102"


def _csrf(html):
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def _key(html):
    return re.search(r'name="key" value="([^"]+)"', html).group(1)


def _staff(app, db_path, username, role):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR IGNORE INTO users (username, password_hash, role, active)"
                 " VALUES (?, ?, ?, 1)", (username, generate_password_hash("x"), role))
    conn.commit()
    token = web_session.create_session(conn, username, role)
    conn.close()
    client = app.test_client()
    client.set_cookie(web_session.COOKIE_NAME, token)
    return client


def _patient(papp, db_path, cf):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    patient_auth.issue_pin(cf, conn, "bl_dentist", "dentist")
    conn.execute("UPDATE patient_credentials SET must_change_pin = 0 WHERE patient_id = ?",
                 (patient_id.resolve(conn, cf),))
    conn.commit()
    token = patient_auth.create_patient_session(conn, cf)
    conn.close()
    client = papp.test_client()
    client.set_cookie(patient_auth.PATIENT_COOKIE_NAME, token)
    return client


def _visit(conn, pid, day, lines):
    vid = conn.execute("INSERT INTO visits (patient_id, visit_date, procedures, clinical_notes,"
                       " source_path) VALUES (?, ?, '[]', '', ?)", (pid, day, f"{pid}{day}")).lastrowid
    for i, (desc, amount) in enumerate(lines):
        conn.execute("INSERT INTO invoices (patient_id, visit_id, line_index, amount,"
                     " amount_cents, description) VALUES (?, ?, ?, ?, ?, ?)",
                     (pid, vid, i, amount / 100, amount, desc))
    return vid


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "clinic.sqlite")
        app_db.DB_PATH = db_path
        app_db.CHROMA_PATH = str(Path(tmp) / "chroma")
        app = create_app()
        app.config["TESTING"] = True
        patient_routes.DB_PATH = db_path
        papp = create_patient_app(env_path=Path(tmp) / ".env.patient")
        papp.config["TESTING"] = True

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        pid = patient_id.seed_patient(conn, CF, "Bianca Billing", "3331112233")
        other = patient_id.seed_patient(conn, OTHER, "Oscar Other", None)
        legacy_visit = _visit(conn, pid, "2025-11-10", [("igiene", 8000)])
        new_visit = _visit(conn, pid, "2026-09-01", [("cura canalare", 34000), ("radiografia", 4500)])
        _visit(conn, other, "2026-09-02", [("otturazione", 12000)])
        legacy = ledger.ensure_invoice(conn, pid, legacy_visit, legacy=True)
        draft = ledger.ensure_invoice(conn, pid, new_visit)
        conn.commit()
        conn.close()

        dentist = _staff(app, db_path, "bl_dentist", "dentist")
        assistant = _staff(app, db_path, "bl_assist", "assistant")
        page = dentist.get(f"/patients/{CF}/billing").text
        assert "unknown" in page and "Reconcile" in page and "Issue" in page, "1: the forms show"

        # 1. reconcile the legacy invoice as partly paid, issue the new one
        dentist.post(f"/billing/invoices/{legacy}/reconcile", data={
            "outcome": "partial", "amount": "50,00", "method": "cash", "key": _key(page),
            "csrf_token": _csrf(page)})
        dentist.post(f"/billing/invoices/{draft}/issue", data={
            "due_date": "2026-10-15", "csrf_token": _csrf(page)})
        page = dentist.get(f"/patients/{CF}/billing").text
        assert "partially paid" in page and "Record payment" in page, "1: reconciled and issued"

        # 2. the same form submitted twice is one payment
        key = _key(page)
        for _ in range(2):
            dentist.post(f"/billing/invoices/{draft}/payment", data={
                "amount": "100,00", "method": "card", "key": key, "reference": "R-0001",
                "csrf_token": _csrf(page)})
        conn = sqlite3.connect(db_path)
        n = conn.execute("SELECT COUNT(*) FROM payments WHERE reference = 'R-0001'").fetchone()[0]
        conn.close()
        assert n == 1, f"2: a double submit recorded {n} payments"

        # 3. installments over what is left; the preview says what it is not
        page = dentist.get(f"/patients/{CF}/billing").text
        dentist.post(f"/billing/invoices/{draft}/plan", data={
            "count": "3", "first_due": "2026-10-15", "csrf_token": _csrf(page)})
        preview = dentist.get(f"/billing/invoices/{draft}/preview").text
        assert "not a fiscal document" in preview and "€285.00" in preview, "3: preview"

        # 4. reception records a payment received and nothing else (owner
        # decision 2026-09-22): the payment form only, idempotent, audited
        seen = assistant.get(f"/patients/{CF}/billing").text
        assert "Record payment" in seen, "4: reception is offered the payment form"
        for dentist_only in ("Refund", "Reverse", "Reconcile", "Split", ">Void<", ">Issue<"):
            assert dentist_only not in seen, f"4: reception must not be offered {dentist_only}"
        for _ in range(2):
            assistant.post(f"/billing/invoices/{draft}/payment", data={
                "amount": "5,00", "method": "cash", "key": "asst-1", "csrf_token": _csrf(seen)})
        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT COUNT(*) FROM payments WHERE idempotency_key = 'asst-1'"
                            " AND recorded_role = 'assistant'").fetchone()[0] == 1, \
            "4: reception's payment is recorded once, under its own role"
        asst_pay = conn.execute("SELECT id FROM payments WHERE idempotency_key = 'asst-1'").fetchone()[0]
        conn.close()
        before = sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM payments").fetchone()[0]
        for url in (f"/billing/payments/{asst_pay}/refund", f"/billing/payments/{asst_pay}/reverse",
                    f"/billing/invoices/{draft}/void", f"/billing/invoices/{draft}/plan",
                    f"/billing/invoices/{legacy}/reconcile"):
            assistant.post(url, data={"amount": "1", "reason": "x", "outcome": "paid", "count": "2",
                                      "first_due": "2026-12-01", "key": f"asst-{url}",
                                      "csrf_token": _csrf(seen)})
        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == before, \
            "4: reception's refund, reversal or reconciliation must not land"
        refused = conn.execute("SELECT COUNT(*) FROM audit_log WHERE username = 'bl_assist'"
                               " AND allowed = 0").fetchone()[0]
        assert refused == 5, f"4: each refused attempt is audited, got {refused}"
        # the 5,00 goes back out, by a dentist, so the figures below are unchanged
        dentist.post(f"/billing/payments/{asst_pay}/reverse", data={
            "reason": "test fixture", "key": "undo-asst-1", "csrf_token": _csrf(page)})
        conn.close()

        # 5. P07.T2 - one amount, the same on every surface
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        owed = ledger.patient_summary(conn, pid)["outstanding_cents"]
        assert owed == 3000 + 28500, f"5: expected 315.00 outstanding, got {owed}"
        staff_page = dentist.get(f"/patients/{CF}/billing").text
        qa = ask.answer_exact(CF, "invoice", conn)
        portal = _patient(papp, db_path, CF)
        portal_page = portal.get("/billing").get_data(as_text=True)
        chat_it = chat.invoice_answer(ledger.patient_summary(conn, pid), "it")
        en, it = ledger.fmt(owed, "en"), ledger.fmt(owed, "it")
        assert f"Still to pay {en}" in staff_page, "5: staff page"
        assert f"still to pay {en}" in qa, f"5: staff Q&A: {qa}"
        assert it in portal_page, "5: portal"
        assert f"restano da pagare {it}" in chat_it, f"5: chat: {chat_it}"
        assert "Prossima rata" in chat_it, "5: the chat names the next installment"

        # 6. P07.T4 - the portal shows only the session patient's invoices
        assert "otturazione" not in portal_page and "Oscar" not in portal_page, "6: leak"
        portal.get("/lang/en")
        assert "You cannot pay through this portal" in portal.get("/billing").get_data(as_text=True), \
            "6: the portal says no money passes through it"
        conn.close()

        # 6b. a ledger row that points this patient at another patient's visit
        # is a scope violation: the accessor returns nothing and logs it
        import patient_accessor
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        foreign = conn.execute("SELECT id FROM visits WHERE patient_id = ?", (other,)).fetchone()[0]
        conn.execute("INSERT INTO billing_invoices (patient_id, visit_id, state, created_at)"
                     " VALUES (?, ?, 'draft', '2026-09-22T08:00:00+00:00')", (pid, foreign))
        conn.commit()
        before = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action ="
                              " 'patient_scope_violation'").fetchone()[0]
        assert patient_accessor.get_billing(pid, conn) is None, "6b: a foreign invoice was served"
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action ="
                            " 'patient_scope_violation'").fetchone()[0] == before + 1, "6b: logged"
        conn.execute("DELETE FROM billing_invoices WHERE visit_id = ?", (foreign,))
        conn.commit()
        conn.close()

        # 7. a refund reopens what is owed, and void is refused with money on it
        conn = sqlite3.connect(db_path)
        pay_id = conn.execute("SELECT id FROM payments WHERE reference = 'R-0001'").fetchone()[0]
        conn.close()
        page = dentist.get(f"/patients/{CF}/billing").text
        dentist.post(f"/billing/payments/{pay_id}/refund", data={
            "amount": "25,00", "reason": "discount agreed", "key": _key(page),
            "csrf_token": _csrf(page)})
        dentist.post(f"/billing/invoices/{draft}/void", data={
            "reason": "test", "csrf_token": _csrf(page)})
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        s = ledger.summary(conn, draft)
        assert (s["state"], s["outstanding_cents"]) == ("partially_paid", 31000), s
        audits = {r[0] for r in conn.execute("SELECT action FROM audit_log WHERE target = ?",
                                              (pid,))}
        for action in ("reconcile_invoice", "issue_invoice", "record_payment",
                       "plan_installments", "refund_payment", "view_billing", "preview_invoice"):
            assert action in audits, f"7: {action} is not audited"
        conn.close()

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python billing_routes_selftest.py --selftest")
    sys.exit(1)


if __name__ == "__main__":
    main()
