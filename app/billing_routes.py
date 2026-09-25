import secrets

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

import ledger
import patient_id
from auth import authorize, log_audit
from codice_fiscale import is_valid as is_valid_cf
from storage import lookup_patient

from .db import get_db

billing_bp = Blueprint("billing", __name__)

INVOICE_ACTIONS = ("issue", "void", "reconcile", "plan")
PAYMENT_ACTIONS = ("refund", "reverse")


def _refuse(action, target=None):
    log_audit(get_db(), g.user["username"], g.user["role"], action, target, allowed=0)
    flash("You don't have permission to do that with billing.", "danger")
    return redirect(url_for("dashboard.index"))


def _patient_url(pid):
    row = get_db().execute("SELECT codice_fiscale FROM patients WHERE patient_id = ?",
                           (pid,)).fetchone()
    if row is None:
        return url_for("billing.index")
    return url_for("billing.patient", cf=row["codice_fiscale"])


@billing_bp.route("/billing")
def index():
    if not authorize(g.user["role"], "view_billing"):
        return _refuse("view_billing")
    conn = get_db()
    rows = conn.execute(
        "SELECT b.id, b.patient_id, p.patient_name, p.codice_fiscale FROM billing_invoices b"
        " JOIN patients p ON p.patient_id = b.patient_id ORDER BY b.id").fetchall()
    work = {"unknown": [], "draft": [], "owed": []}
    for r in rows:
        s = ledger.summary(conn, r["id"])
        s.update(patient_name=r["patient_name"], cf=r["codice_fiscale"])
        if s["state"] == "unknown":
            work["unknown"].append(s)
        elif s["state"] == "draft":
            work["draft"].append(s)
        elif s["outstanding_cents"] > 0:
            work["owed"].append(s)
    work["owed"].sort(key=lambda s: s["due_date"] or "")
    recorded = conn.execute(
        "SELECT COALESCE(SUM(CASE kind WHEN 'payment' THEN amount_cents ELSE -amount_cents END), 0)"
        " FROM payments").fetchone()[0]
    return render_template("billing.html", work=work, fmt=ledger.fmt, recorded=recorded,
                           owed=sum(s["outstanding_cents"] for s in work["owed"]),
                           can_change=authorize(g.user["role"], "manage_billing"),
                           can_record=authorize(g.user["role"], "record_payment"))


@billing_bp.route("/patients/<cf>/billing")
def patient(cf):
    if not authorize(g.user["role"], "view_billing"):
        return _refuse("view_billing", cf)
    if not is_valid_cf(cf):
        abort(404)
    conn = get_db()
    pid = patient_id.resolve(conn, cf)
    person = lookup_patient(cf, conn)
    if pid is None or person is None:
        abort(404)
    log_audit(conn, g.user["username"], g.user["role"], "view_billing", pid, allowed=1)
    return render_template(
        "billing_patient.html", cf=cf, patient=person, summary=ledger.patient_summary(conn, pid),
        payments=ledger.payments_for(conn, pid), fmt=ledger.fmt, methods=ledger.METHODS,
        order=ledger.ALLOCATION_ORDER, key=lambda: secrets.token_urlsafe(16),
        can_change=authorize(g.user["role"], "manage_billing"),
        can_pay=authorize(g.user["role"], "record_payment"))


@billing_bp.route("/billing/invoices/<int:invoice_id>/<action>", methods=["POST"])
def invoice_action(invoice_id, action):
    if not authorize(g.user["role"], "manage_billing"):
        return _refuse(f"billing_{action}", str(invoice_id))
    if action not in INVOICE_ACTIONS:
        abort(404)
    conn = get_db()
    inv = conn.execute("SELECT patient_id FROM billing_invoices WHERE id = ?",
                       (invoice_id,)).fetchone()
    if inv is None:
        abort(404)
    who, role, form = g.user["username"], g.user["role"], request.form
    try:
        if action == "issue":
            ledger.issue(conn, invoice_id, who, role, form.get("due_date") or None)
            message = "Invoice issued (internal - no fiscal document is produced)."
        elif action == "void":
            ledger.void(conn, invoice_id, who, role, form.get("reason"))
            message = "Invoice voided."
        elif action == "reconcile":
            ledger.reconcile(conn, invoice_id, form.get("outcome"), who, role, form.get("key"),
                             amount=form.get("amount") or None, method=form.get("method") or "other",
                             received_on=form.get("received_on") or None, note=form.get("note"))
            message = "Invoice reconciled."
        else:
            ledger.plan_installments(conn, invoice_id, form.get("count", "0"),
                                     form.get("first_due", ""), who, role)
            message = "Installment plan created."
        flash(message, "success")
    except (ledger.LedgerError, ValueError) as e:
        flash(f"Not done: {e}.", "danger")
    return redirect(_patient_url(inv["patient_id"]))


@billing_bp.route("/billing/invoices/<int:invoice_id>/payment", methods=["POST"])
def record_payment(invoice_id):
    # reception may record money received (owner decision 2026-09-22); every
    # other change to money stays behind manage_billing in invoice_action
    if not authorize(g.user["role"], "record_payment"):
        return _refuse("record_payment", str(invoice_id))
    conn = get_db()
    inv = conn.execute("SELECT patient_id FROM billing_invoices WHERE id = ?",
                       (invoice_id,)).fetchone()
    if inv is None:
        abort(404)
    form = request.form
    try:
        _, created = ledger.record_payment(
            conn, invoice_id, form.get("amount"), form.get("method"), form.get("key"),
            g.user["username"], g.user["role"], form.get("received_on") or None,
            form.get("reference") or None, form.get("note") or None)
        flash("Payment recorded." if created else "That payment was already recorded.", "success")
    except (ledger.LedgerError, ValueError) as e:
        flash(f"Not done: {e}.", "danger")
    return redirect(_patient_url(inv["patient_id"]))


@billing_bp.route("/billing/payments/<int:payment_id>/<action>", methods=["POST"])
def payment_action(payment_id, action):
    if not authorize(g.user["role"], "manage_billing"):
        return _refuse(f"billing_{action}", str(payment_id))
    if action not in PAYMENT_ACTIONS:
        abort(404)
    conn = get_db()
    row = conn.execute("SELECT patient_id FROM payments WHERE id = ?", (payment_id,)).fetchone()
    if row is None:
        abort(404)
    who, role, form = g.user["username"], g.user["role"], request.form
    try:
        if action == "refund":
            _, created = ledger.refund(conn, payment_id, form.get("amount"), form.get("key"),
                                       who, role, form.get("reason"))
        else:
            _, created = ledger.reverse(conn, payment_id, form.get("key"), who, role,
                                        form.get("reason"))
        flash(("Refund recorded." if action == "refund" else "Payment reversed.")
              if created else "That was already recorded.", "success")
    except (ledger.LedgerError, ValueError) as e:
        flash(f"Not done: {e}.", "danger")
    return redirect(_patient_url(row["patient_id"]))


@billing_bp.route("/billing/invoices/<int:invoice_id>/preview")
def preview(invoice_id):
    if not authorize(g.user["role"], "view_billing"):
        return _refuse("view_billing", str(invoice_id))
    conn = get_db()
    s = ledger.summary(conn, invoice_id)
    if s is None:
        abort(404)
    person = conn.execute("SELECT patient_name, codice_fiscale FROM patients WHERE patient_id = ?",
                          (s["patient_id"],)).fetchone()
    log_audit(conn, g.user["username"], g.user["role"], "preview_invoice", s["patient_id"],
              allowed=1, reason=str(invoice_id))
    return render_template("billing_preview.html", inv=s, person=person, fmt=ledger.fmt)
