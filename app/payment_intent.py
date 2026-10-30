import secrets
import json
from datetime import timedelta

from flask import current_app
from itsdangerous import BadData, URLSafeSerializer

import clinic_time
import ledger
import pending_actions


def _signer():
    return URLSafeSerializer(current_app.secret_key, salt="manual-payment-v1")


def create(conn, invoice, user):
    now = clinic_time.now_utc()
    return _signer().dumps({
        "invoice": invoice["id"], "patient": invoice["patient_id"],
        "actor": user["username"], "role": user["role"],
        "key": "manual-" + secrets.token_urlsafe(24),
        "revision": ledger.payment_revision(conn, invoice["id"]),
        "received_on": clinic_time.to_local(now).date().isoformat(),
        "expires": (now + timedelta(minutes=pending_actions.PENDING_ACTION_EXPIRY_MINUTES)).isoformat(),
    })


def stage(conn, invoice, user):
    """Store only form metadata, using the existing short-lived draft store.

    The numeric row ID is a view locator, never write authority. The signed
    intent goes only into the authenticated form body. Do NOT consume this
    draft with consume_pending_action: the ledger owns atomic consumption.
    """
    token = create(conn, invoice, user)
    handle = pending_actions.create_pending_action(
        conn, user["username"], user["role"], {"kind": "manual-payment-v1", "intent": token})
    return conn.execute("SELECT id FROM pending_actions WHERE token_hash = ?",
                        (pending_actions._hash_token(handle),)).fetchone()[0]


def load_form(conn, form_id, invoice, user):
    row = conn.execute("SELECT payload FROM pending_actions WHERE id = ? AND username = ? AND role = ?",
                       (form_id, user["username"], user["role"])).fetchone()
    if row is None:
        raise ledger.LedgerError("this payment form is no longer available; check the invoice before starting again")
    payload = json.loads(row["payload"])
    if payload.get("kind") != "manual-payment-v1":
        raise ledger.LedgerError("this is not a payment form")
    token = payload["intent"]
    read(token, invoice, user)
    return token


def receipt_for_id(conn, payment_id, invoice, user):
    return conn.execute(
        "SELECT p.* FROM payments p JOIN payment_allocations a ON a.payment_id = p.id"
        " WHERE p.id = ? AND p.recorded_by = ? AND p.recorded_role = ?"
        " AND p.patient_id = ? AND a.invoice_id = ? AND p.kind = 'payment'",
        (payment_id, user["username"], user["role"], invoice["patient_id"], invoice["id"]),
    ).fetchone()


def read(token, invoice, user):
    if not isinstance(token, str) or not token or len(token) > 2048:
        raise ledger.LedgerError("start a payment from the invoice first")
    try:
        intent = _signer().loads(token)
    except BadData:
        raise ledger.LedgerError("this payment form is invalid; start again from the invoice") from None
    if (intent["invoice"] != invoice["id"] or intent["patient"] != invoice["patient_id"]
            or intent["actor"] != user["username"] or intent["role"] != user["role"]):
        raise ledger.LedgerError("this payment form belongs to a different invoice or staff account")
    return intent


def receipt(conn, intent):
    return conn.execute(
        "SELECT p.* FROM payments p JOIN payment_allocations a ON a.payment_id = p.id"
        " WHERE p.idempotency_key = ? AND p.recorded_by = ? AND p.recorded_role = ?"
        " AND p.patient_id = ? AND a.invoice_id = ? AND p.kind = 'payment'",
        (intent["key"], intent["actor"], intent["role"], intent["patient"], intent["invoice"]),
    ).fetchone()


def check_open(conn, intent):
    if clinic_time.now_utc() > clinic_time.read_instant(intent["expires"]):
        raise ledger.LedgerError("this payment form has expired; check the invoice before starting again")
    if ledger.payment_revision(conn, intent["invoice"]) != intent["revision"]:
        raise ledger.LedgerError("the invoice payments have changed; check the invoice before starting again")
