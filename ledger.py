"""Payment ledger: invoices, manual payments, refunds, installments (P07).

No payment provider. Every payment here is one a person typed in after money
changed hands somewhere else; nothing in this module moves or receives money,
and the screens say so.

The lines of an invoice are the `invoices` rows a filed note produced, one
invoice per visit. Money is integer cents throughout; the old REAL `amount`
column is kept for what already reads it and is never summed here.

State of an invoice:
    unknown         existed before the ledger; paid or not is not recorded.
                    never counted as owed until someone reconciles it
    draft           built from a note, not yet issued; not owed
    issued / partially_paid / paid   derived from what is allocated to it
    void            cancelled, with a reason; nothing owed

Payments, allocations and events are append-only. A mistaken payment is
reversed by a new row, a refund is a new row, and neither edits the original.

summary() and patient_summary() are the only places totals are computed. The
staff pages, the patient portal, the chat and staff Q&A all read them.
"""
import sqlite3
import sys
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import clinic_time
from auth import authorize, log_audit

CURRENCY = "EUR"
METHODS = ("cash", "card", "bank_transfer", "other")
DUE_DAYS = 30
# the allocation rule, approved by the owner 2026-09-22 (P07.04): money on an
# invoice covers its oldest unpaid installment first - due date, then sequence
# for a tie. nothing stores which installment a payment "went to": coverage is
# derived from the append-only allocations every time, so the rule cannot drift
# from the history, and a refund reopens the most recently covered one first.
ALLOCATION_ORDER = "oldest unpaid installment first"

SCHEMA = """
    CREATE TABLE IF NOT EXISTS billing_invoices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT NOT NULL REFERENCES patients(patient_id),
        visit_id INTEGER NOT NULL UNIQUE REFERENCES visits(id),
        currency TEXT NOT NULL DEFAULT 'EUR',
        state TEXT NOT NULL CHECK (state IN ('unknown', 'draft', 'issued', 'void')),
        issued_at TEXT,
        due_date TEXT,
        reconciled_at TEXT,
        reconciled_by TEXT,
        void_reason TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_billing_invoices_patient ON billing_invoices (patient_id);
    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT NOT NULL REFERENCES patients(patient_id),
        kind TEXT NOT NULL CHECK (kind IN ('payment', 'refund', 'reversal')),
        amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
        method TEXT NOT NULL,
        source TEXT NOT NULL CHECK (source IN ('manual', 'reconciliation', 'demo_fixture', 'provider')),
        idempotency_key TEXT NOT NULL UNIQUE,
        reference TEXT,
        reverses_payment_id INTEGER REFERENCES payments(id),
        received_on TEXT NOT NULL,
        recorded_by TEXT NOT NULL,
        recorded_role TEXT NOT NULL,
        note TEXT,
        ts TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_payments_patient ON payments (patient_id);
    CREATE TABLE IF NOT EXISTS payment_allocations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        payment_id INTEGER NOT NULL REFERENCES payments(id),
        invoice_id INTEGER NOT NULL REFERENCES billing_invoices(id),
        amount_cents INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_allocations_invoice ON payment_allocations (invoice_id);
    CREATE TABLE IF NOT EXISTS installment_plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invoice_id INTEGER NOT NULL UNIQUE REFERENCES billing_invoices(id),
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS installments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id INTEGER NOT NULL REFERENCES installment_plans(id),
        seq INTEGER NOT NULL,
        due_date TEXT NOT NULL,
        amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
        UNIQUE (plan_id, seq)
    );
    CREATE TABLE IF NOT EXISTS billing_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event TEXT NOT NULL CHECK (event IN ('invoice_issued', 'installment_due')),
        patient_id TEXT NOT NULL,
        invoice_id INTEGER NOT NULL,
        installment_id INTEGER,
        due_date TEXT,
        created_at TEXT NOT NULL,
        consumed_at TEXT
    );
"""

# history is never edited in place. the unlock row is the same sanctioned
# door the audit trail uses: only an approved erasure opens it.
APPEND_ONLY = """
    CREATE TRIGGER IF NOT EXISTS payments_no_update BEFORE UPDATE ON payments
    BEGIN SELECT RAISE(ABORT, 'payments are append-only'); END;
    CREATE TRIGGER IF NOT EXISTS payments_no_delete BEFORE DELETE ON payments
    WHEN NOT EXISTS (SELECT 1 FROM audit_unlock)
    BEGIN SELECT RAISE(ABORT, 'payments are append-only'); END;
    CREATE TRIGGER IF NOT EXISTS allocations_no_update BEFORE UPDATE ON payment_allocations
    BEGIN SELECT RAISE(ABORT, 'allocations are append-only'); END;
    CREATE TRIGGER IF NOT EXISTS allocations_no_delete BEFORE DELETE ON payment_allocations
    WHEN NOT EXISTS (SELECT 1 FROM audit_unlock)
    BEGIN SELECT RAISE(ABORT, 'allocations are append-only'); END;
"""


class LedgerError(ValueError):
    pass


# --- money ----------------------------------------------------------------


def cents(value):
    """A euro amount as typed or stored -> integer cents, rounded half up.
    Goes through the decimal text, so 0.1 + 0.2 never becomes 30.000000004."""
    if value is None or str(value).strip() == "":
        raise LedgerError("an amount is required")
    text = str(value).strip().replace("€", "").replace(" ", "")
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        amount = Decimal(text)
    except Exception:
        raise LedgerError(f"not an amount: {value!r}")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fmt(value_cents, lang="it"):
    sign = "-" if value_cents < 0 else ""
    whole, part = divmod(abs(value_cents), 100)
    grouped = f"{whole:,}"
    if lang == "it":
        return f"{sign}€ {grouped.replace(',', '.')},{part:02d}"
    return f"{sign}€{grouped}.{part:02d}"


# --- reading --------------------------------------------------------------


def lines(conn, invoice):
    return conn.execute(
        "SELECT line_index, description, amount_cents FROM invoices WHERE visit_id = ?"
        " ORDER BY line_index", (invoice["visit_id"],)).fetchall()


def summary(conn, invoice_id):
    """Everything known about one invoice's money, computed in one place."""
    inv = conn.execute("SELECT b.*, v.visit_date FROM billing_invoices b"
                       " JOIN visits v ON v.id = b.visit_id WHERE b.id = ?",
                       (invoice_id,)).fetchone()
    if inv is None:
        return None
    rows = lines(conn, inv)
    total = sum(r["amount_cents"] for r in rows)
    paid = conn.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM payment_allocations"
                        " WHERE invoice_id = ?", (invoice_id,)).fetchone()[0]
    state = inv["state"]
    if state == "issued":
        if paid <= 0:
            state = "issued"
        elif paid < total:
            state = "partially_paid"
        else:
            state = "paid"
    owed = total - paid if state in ("issued", "partially_paid") else 0
    plan = _installments(conn, invoice_id, paid)
    return {
        "id": inv["id"], "patient_id": inv["patient_id"], "visit_id": inv["visit_id"],
        "visit_date": inv["visit_date"], "currency": inv["currency"], "state": state,
        "issued_at": inv["issued_at"], "due_date": inv["due_date"],
        "void_reason": inv["void_reason"], "lines": [dict(r) for r in rows],
        "total_cents": total, "paid_cents": paid, "outstanding_cents": owed,
        "installments": plan,
        "installments_left": sum(1 for i in plan if i["open_cents"] > 0),
    }


def _installments(conn, invoice_id, paid):
    rows = conn.execute(
        "SELECT i.* FROM installments i JOIN installment_plans p ON p.id = i.plan_id"
        " WHERE p.invoice_id = ? ORDER BY i.due_date, i.seq", (invoice_id,)).fetchall()
    out = []
    left = paid
    for r in rows:
        covered = min(max(left, 0), r["amount_cents"])
        left -= covered
        out.append({"id": r["id"], "seq": r["seq"], "due_date": r["due_date"],
                    "amount_cents": r["amount_cents"], "open_cents": r["amount_cents"] - covered})
    return out


def _unlinked(conn, pid):
    # lines with no ledger invoice predate the ledger (or migrate_ledger has
    # not run): shown, never owed - the same as an unknown invoice
    out = []
    for v in conn.execute("SELECT DISTINCT v.id, v.visit_date FROM invoices i JOIN visits v"
                          " ON v.id = i.visit_id WHERE i.patient_id = ? AND i.visit_id NOT IN"
                          " (SELECT visit_id FROM billing_invoices)", (pid,)).fetchall():
        rows = lines(conn, {"visit_id": v["id"]})
        out.append({"id": None, "patient_id": pid, "visit_id": v["id"],
                    "visit_date": v["visit_date"], "currency": CURRENCY, "state": "unknown",
                    "issued_at": None, "due_date": None, "void_reason": None,
                    "lines": [dict(r) for r in rows],
                    "total_cents": sum(r["amount_cents"] for r in rows), "paid_cents": 0,
                    "outstanding_cents": 0, "installments": [], "installments_left": 0})
    return out


def patient_summary(conn, pid):
    ids = [r[0] for r in conn.execute(
        "SELECT b.id FROM billing_invoices b JOIN visits v ON v.id = b.visit_id"
        " WHERE b.patient_id = ? ORDER BY v.visit_date, b.id", (pid,))]
    invoices = [summary(conn, i) for i in ids] + _unlinked(conn, pid)
    invoices.sort(key=lambda i: (i["visit_date"] or "", i["visit_id"]))
    return {
        "invoices": invoices,
        "outstanding_cents": sum(i["outstanding_cents"] for i in invoices),
        "unknown": sum(1 for i in invoices if i["state"] == "unknown"),
        "next_due": min((x["due_date"] for i in invoices for x in i["installments"]
                         if x["open_cents"] > 0), default=None),
    }


def payments_for(conn, pid):
    return conn.execute("SELECT * FROM payments WHERE patient_id = ? ORDER BY id",
                        (pid,)).fetchall()


# --- invoices from notes --------------------------------------------------


def ensure_invoice(conn, pid, visit_id, legacy=False):
    """Called by storage when a note brings invoice lines. A new visit's
    invoice starts as a draft; the legacy migration creates unknown ones."""
    row = conn.execute("SELECT id FROM billing_invoices WHERE visit_id = ?",
                       (visit_id,)).fetchone()
    if row:
        return row[0]
    cur = conn.execute(
        "INSERT INTO billing_invoices (patient_id, visit_id, state, created_at)"
        " VALUES (?, ?, ?, ?)", (pid, visit_id, "unknown" if legacy else "draft",
                                 clinic_time.stamp()))
    return cur.lastrowid


def lines_locked(conn, visit_id):
    """True when an invoice's lines may no longer change under it: issued,
    void, or with money allocated. A note re-sync then leaves them alone."""
    row = conn.execute("SELECT id, state FROM billing_invoices WHERE visit_id = ?",
                       (visit_id,)).fetchone()
    if row is None:
        return False
    if row["state"] in ("issued", "void"):
        return True
    return conn.execute("SELECT 1 FROM payment_allocations WHERE invoice_id = ?",
                        (row["id"],)).fetchone() is not None


# --- changing -------------------------------------------------------------


def _gate(conn, actor, role, action, target, capability="manage_billing"):
    if not authorize(role, capability):
        log_audit(conn, actor, role, action, target, allowed=0)
        raise PermissionError(f"{role} may not {action.replace('_', ' ')}")


def _invoice(conn, invoice_id):
    inv = conn.execute("SELECT * FROM billing_invoices WHERE id = ?", (invoice_id,)).fetchone()
    if inv is None:
        raise LedgerError("no such invoice")
    return inv


def issue(conn, invoice_id, actor, role, due_date=None):
    _gate(conn, actor, role, "issue_invoice", str(invoice_id))
    inv = _invoice(conn, invoice_id)
    if inv["state"] != "draft":
        raise LedgerError(f"only a draft can be issued, this one is {inv['state']}")
    if not lines(conn, inv):
        raise LedgerError("an invoice with no lines cannot be issued")
    due = due_date or (clinic_time.now_utc().date() + timedelta(days=DUE_DAYS)).isoformat()
    date.fromisoformat(due)
    conn.execute("UPDATE billing_invoices SET state = 'issued', issued_at = ?, due_date = ?"
                 " WHERE id = ?", (clinic_time.stamp(), due, invoice_id))
    _event(conn, "invoice_issued", inv["patient_id"], invoice_id, None, due)
    conn.commit()
    log_audit(conn, actor, role, "issue_invoice", inv["patient_id"], allowed=1,
              reason=str(invoice_id))


def void(conn, invoice_id, actor, role, reason):
    _gate(conn, actor, role, "void_invoice", str(invoice_id))
    inv = _invoice(conn, invoice_id)
    reason = (reason or "").strip()[:300]
    if not reason:
        raise LedgerError("a void needs a reason")
    if inv["state"] == "void":
        raise LedgerError("already void")
    if summary(conn, invoice_id)["paid_cents"] != 0:
        raise LedgerError("money is allocated to this invoice: refund or reverse it first")
    conn.execute("UPDATE billing_invoices SET state = 'void', void_reason = ? WHERE id = ?",
                 (reason, invoice_id))
    conn.commit()
    log_audit(conn, actor, role, "void_invoice", inv["patient_id"], allowed=1,
              reason=str(invoice_id))


def _insert_payment(conn, pid, kind, amount, method, source, key, actor, role, received_on,
                    reference=None, note=None, reverses=None):
    cur = conn.execute(
        "INSERT INTO payments (patient_id, kind, amount_cents, method, source, idempotency_key,"
        " reference, reverses_payment_id, received_on, recorded_by, recorded_role, note, ts)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (pid, kind, amount, method, source, key, reference, reverses, received_on, actor, role,
         note, clinic_time.stamp()))
    return cur.lastrowid


def _existing(conn, key):
    return conn.execute("SELECT id FROM payments WHERE idempotency_key = ?", (key,)).fetchone()


def record_payment(conn, invoice_id, amount, method, key, actor, role, received_on=None,
                   reference=None, note=None, source="manual"):
    """Record money received against one invoice. Returns (payment_id, created).
    The same idempotency key twice returns the first payment and records nothing."""
    # the one change reception may make (owner decision 2026-09-22): recording
    # money received. refunds, reversals, reconciliation, issue, void and plans
    # stay behind manage_billing
    _gate(conn, actor, role, "record_payment", str(invoice_id), "record_payment")
    if not key:
        raise LedgerError("a payment needs an idempotency key")
    found = _existing(conn, key)
    if found:
        return found[0], False
    amount = cents(amount) if not isinstance(amount, int) else amount
    if amount <= 0:
        raise LedgerError("a payment is more than zero")
    if method not in METHODS:
        raise LedgerError(f"method must be one of {METHODS}")
    received_on = received_on or clinic_time.now_utc().date().isoformat()
    date.fromisoformat(received_on)

    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        # re-read inside the lock: two tabs submitting at once see each other
        found = _existing(conn, key)
        if found:
            conn.execute("ROLLBACK")
            return found[0], False
        state = summary(conn, invoice_id)
        if state is None:
            raise LedgerError("no such invoice")
        if state["state"] not in ("issued", "partially_paid"):
            raise LedgerError(f"payments go against an issued invoice, this one is {state['state']}")
        if amount > state["outstanding_cents"]:
            raise LedgerError(f"that is more than the {fmt(state['outstanding_cents'])} outstanding")
        pid = state["patient_id"]
        payment_id = _insert_payment(conn, pid, "payment", amount, method, source, key, actor,
                                     role, received_on, reference, note)
        conn.execute("INSERT INTO payment_allocations (payment_id, invoice_id, amount_cents)"
                     " VALUES (?, ?, ?)", (payment_id, invoice_id, amount))
        conn.execute("COMMIT")
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK")
        found = _existing(conn, key)
        if found:
            return found[0], False
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise
    log_audit(conn, actor, role, "record_payment", pid, allowed=1, reason=str(payment_id))
    return payment_id, True


def _allocated(conn, payment_id):
    return conn.execute("SELECT invoice_id, COALESCE(SUM(amount_cents), 0) AS c"
                        " FROM payment_allocations WHERE payment_id = ? GROUP BY invoice_id",
                        (payment_id,)).fetchall()


def _returned(conn, payment_id):
    return conn.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM payments"
                        " WHERE reverses_payment_id = ?", (payment_id,)).fetchone()[0]


def refund(conn, payment_id, amount, key, actor, role, reason, method=None):
    """Money handed back. A new row against the original; the invoice owes
    that much again. Returns (refund_id, created)."""
    return _counter(conn, payment_id, amount, key, actor, role, reason, "refund", method)


def reverse(conn, payment_id, key, actor, role, reason):
    """A payment entered by mistake is taken out whole by a new row. The
    original stays in the history."""
    return _counter(conn, payment_id, None, key, actor, role, reason, "reversal", None)


def _counter(conn, payment_id, amount, key, actor, role, reason, kind, method):
    action = "refund_payment" if kind == "refund" else "reverse_payment"
    _gate(conn, actor, role, action, str(payment_id))
    reason = (reason or "").strip()[:300]
    if not reason:
        raise LedgerError(f"a {kind} needs a reason")
    found = _existing(conn, key)
    if found:
        return found[0], False
    original = conn.execute("SELECT * FROM payments WHERE id = ? AND kind = 'payment'",
                            (payment_id,)).fetchone()
    if original is None:
        raise LedgerError("no such payment")
    left = original["amount_cents"] - _returned(conn, payment_id)
    amount = left if amount is None else (cents(amount) if not isinstance(amount, int) else amount)
    if amount <= 0 or amount > left:
        raise LedgerError(f"at most {fmt(left)} of this payment can still be {kind}ed")

    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        new_id = _insert_payment(conn, original["patient_id"], kind, amount,
                                 method or original["method"], "manual", key, actor, role,
                                 clinic_time.now_utc().date().isoformat(), None, reason,
                                 payment_id)
        for alloc in _allocated(conn, payment_id):
            take = min(amount, alloc["c"])
            if take:
                conn.execute("INSERT INTO payment_allocations (payment_id, invoice_id,"
                             " amount_cents) VALUES (?, ?, ?)", (new_id, alloc["invoice_id"], -take))
                amount -= take
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    log_audit(conn, actor, role, action, original["patient_id"], allowed=1, reason=str(new_id))
    return new_id, True


def reconcile(conn, invoice_id, outcome, actor, role, key, amount=None, method="other",
              received_on=None, note=None):
    """Settle an unknown (pre-ledger) invoice from what a person checked:
    'unpaid' makes it issued and owed, 'paid' records it paid in full,
    'partial' records the amount given. The source says reconciliation."""
    _gate(conn, actor, role, "reconcile_invoice", str(invoice_id))
    inv = _invoice(conn, invoice_id)
    if inv["state"] != "unknown":
        raise LedgerError("only an invoice of unknown status is reconciled")
    if outcome not in ("unpaid", "paid", "partial"):
        raise LedgerError("outcome is unpaid, paid or partial")
    now = clinic_time.stamp()
    due = inv["due_date"] or (clinic_time.now_utc().date() + timedelta(days=DUE_DAYS)).isoformat()
    conn.execute("UPDATE billing_invoices SET state = 'issued', issued_at = COALESCE(issued_at, ?),"
                 " due_date = ?, reconciled_at = ?, reconciled_by = ? WHERE id = ?",
                 (now, due, now, actor, invoice_id))
    conn.commit()
    log_audit(conn, actor, role, "reconcile_invoice", inv["patient_id"], allowed=1,
              reason=f"{invoice_id}:{outcome}")
    if outcome == "paid":
        amount = summary(conn, invoice_id)["total_cents"]
    if outcome in ("paid", "partial"):
        try:
            record_payment(conn, invoice_id, amount, method, key, actor, role, received_on,
                           note=note or "recorded at reconciliation", source="reconciliation")
        except Exception:
            # the payment did not land: put the invoice back as it was, so a
            # failed reconciliation cannot turn unknown into owed
            conn.execute("UPDATE billing_invoices SET state = 'unknown', issued_at = ?,"
                         " due_date = ?, reconciled_at = NULL, reconciled_by = NULL WHERE id = ?",
                         (inv["issued_at"], inv["due_date"], invoice_id))
            conn.commit()
            raise


def plan_installments(conn, invoice_id, count, first_due, actor, role, every_days=30):
    """Split what is outstanding into `count` installments, the last one
    absorbing the rounding cent."""
    _gate(conn, actor, role, "plan_installments", str(invoice_id))
    count = int(count)
    if not 2 <= count <= 24:
        raise LedgerError("between 2 and 24 installments")
    state = summary(conn, invoice_id)
    if state is None or state["state"] not in ("issued", "partially_paid"):
        raise LedgerError("installments are for an issued invoice with something outstanding")
    if state["installments"]:
        raise LedgerError("this invoice already has a plan")
    start = date.fromisoformat(first_due)
    owed = state["outstanding_cents"]
    share = owed // count
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        plan = conn.execute("INSERT INTO installment_plans (invoice_id, created_by, created_at)"
                            " VALUES (?, ?, ?)", (invoice_id, actor, clinic_time.stamp())).lastrowid
        for seq in range(count):
            amount = share if seq < count - 1 else owed - share * (count - 1)
            due = (start + timedelta(days=every_days * seq)).isoformat()
            inst = conn.execute("INSERT INTO installments (plan_id, seq, due_date, amount_cents)"
                                " VALUES (?, ?, ?, ?)", (plan, seq + 1, due, amount)).lastrowid
            _event(conn, "installment_due", state["patient_id"], invoice_id, inst, due)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    log_audit(conn, actor, role, "plan_installments", state["patient_id"], allowed=1,
              reason=f"{invoice_id}:{count}")


def _event(conn, event, pid, invoice_id, installment_id, due):
    # for the reminder engine (P09) and integrations (P11) to pick up. nothing
    # here sends anything; consumed_at stays empty until one of them does
    conn.execute("INSERT INTO billing_events (event, patient_id, invoice_id, installment_id,"
                 " due_date, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                 (event, pid, invoice_id, installment_id, due, clinic_time.stamp()))


def selftest():
    import tempfile
    import threading

    import patient_id
    from storage import init_db

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "clinic.sqlite")
        conn = init_db(db_path)
        pid = patient_id.seed_patient(conn, "ZZLG800101010101", "Lia Ledger")
        other = patient_id.seed_patient(conn, "ZZLG800101010102", "Olga Other")

        def visit(who, day, amounts, legacy=False):
            vid = conn.execute("INSERT INTO visits (patient_id, visit_date, procedures,"
                               " clinical_notes, source_path) VALUES (?, ?, '[]', '', ?)",
                               (who, day, f"{who}-{day}.json")).lastrowid
            for i, a in enumerate(amounts):
                conn.execute("INSERT INTO invoices (patient_id, visit_id, line_index, amount,"
                             " amount_cents, description) VALUES (?, ?, ?, ?, ?, 'x')",
                             (who, vid, i, a / 100, a))
            inv = ensure_invoice(conn, who, vid, legacy=legacy)
            conn.commit()
            return inv

        D = ("drossi", "dentist")
        k = iter(f"key-{n}" for n in range(1000))

        # 1. money is cents from text, never a float sum
        assert cents("0,10") + cents("0.20") == 30 and cents("1.234,56") == 123456
        assert cents("12.345") == 1235 and fmt(123456) == "€ 1.234,56" and fmt(5, "en") == "€0.05"

        # 2. a draft is not owed; issuing makes it owed, due in DUE_DAYS
        a = visit(pid, "2026-06-01", [12050, 4950])
        assert summary(conn, a)["state"] == "draft" and patient_summary(conn, pid)["outstanding_cents"] == 0
        issue(conn, a, *D)
        s = summary(conn, a)
        assert (s["state"], s["total_cents"], s["outstanding_cents"]) == ("issued", 17000, 17000), s
        assert conn.execute("SELECT COUNT(*) FROM billing_events WHERE event = 'invoice_issued'"
                            " AND invoice_id = ?", (a,)).fetchone()[0] == 1, "2: issue event"

        # 3. partial, then full; the same key twice is one payment
        p1, made = record_payment(conn, a, "50,00", "card", "k1", *D)
        assert made and summary(conn, a)["state"] == "partially_paid"
        assert record_payment(conn, a, "50,00", "card", "k1", *D) == (p1, False), "3: idempotent"
        assert summary(conn, a)["paid_cents"] == 5000, "3: a repeat must not count twice"
        try:
            record_payment(conn, a, "121,00", "cash", next(k), *D)
            raise AssertionError("3: overpayment must be refused")
        except LedgerError:
            pass
        record_payment(conn, a, "120,00", "cash", next(k), *D)
        s = summary(conn, a)
        assert (s["state"], s["paid_cents"], s["outstanding_cents"]) == ("paid", 17000, 0), s

        # 4. a refund and a reversal are new rows; the invoice owes again
        r, _ = refund(conn, p1, "20,00", next(k), *D, "goodwill discount")
        assert summary(conn, a)["outstanding_cents"] == 2000 and summary(conn, a)["state"] == "partially_paid"
        reverse(conn, p1, next(k), *D, "card terminal double charge")
        assert summary(conn, a)["outstanding_cents"] == 5000, "4: reversal returns the rest of p1"
        try:
            refund(conn, p1, "1,00", next(k), *D, "again")
            raise AssertionError("4: nothing left of p1 to refund")
        except LedgerError:
            pass
        assert conn.execute("SELECT amount_cents FROM payments WHERE id = ?", (p1,)).fetchone()[0] == 5000

        # 5. sum invariant: payments - refunds - reversals = allocated = paid
        net = conn.execute("SELECT SUM(CASE kind WHEN 'payment' THEN amount_cents ELSE -amount_cents END)"
                           " FROM payments WHERE patient_id = ?", (pid,)).fetchone()[0]
        alloc = conn.execute("SELECT SUM(amount_cents) FROM payment_allocations").fetchone()[0]
        assert net == alloc == summary(conn, a)["paid_cents"] == 12000, (net, alloc)

        # 6. void refuses while money is on it, and a voided invoice owes nothing
        b = visit(pid, "2026-07-01", [9000])
        issue(conn, b, *D)
        void(conn, b, *D, "entered twice")
        assert summary(conn, b)["state"] == "void" and summary(conn, b)["outstanding_cents"] == 0
        try:
            void(conn, a, *D, "no")
            raise AssertionError("6: an invoice with money on it cannot be voided")
        except LedgerError:
            pass

        # 7. installments: 3 of 100.00 -> 33.33, 33.33, 33.34, oldest first
        c = visit(pid, "2026-08-01", [10000])
        issue(conn, c, *D)
        plan_installments(conn, c, 3, "2026-10-01", *D)
        plan = summary(conn, c)["installments"]
        assert [i["amount_cents"] for i in plan] == [3333, 3333, 3334], plan
        assert conn.execute("SELECT COUNT(*) FROM billing_events WHERE event = 'installment_due'"
                            " AND invoice_id = ?", (c,)).fetchone()[0] == 3
        record_payment(conn, c, "40,00", "bank_transfer", next(k), *D)
        s = summary(conn, c)
        assert [i["open_cents"] for i in s["installments"]] == [0, 2666, 3334], s["installments"]
        assert s["installments_left"] == 2 and patient_summary(conn, pid)["next_due"] == "2026-10-31"

        # 8. P07.T2 - an unknown invoice is never debt; reconciliation settles it
        u = visit(other, "2025-12-01", [8000], legacy=True)
        ps = patient_summary(conn, other)
        assert ps["outstanding_cents"] == 0 and ps["unknown"] == 1, ps
        try:
            record_payment(conn, u, "80", "cash", next(k), *D)
            raise AssertionError("8: no payment against an unknown invoice")
        except LedgerError:
            pass
        reconcile(conn, u, "partial", *D, next(k), amount="30,00", method="cash")
        s = summary(conn, u)
        assert (s["state"], s["outstanding_cents"]) == ("partially_paid", 5000), s
        assert conn.execute("SELECT source FROM payments WHERE patient_id = ?",
                            (other,)).fetchone()[0] == "reconciliation", "8: the source is recorded"
        u2 = visit(other, "2025-11-01", [1000], legacy=True)
        try:
            reconcile(conn, u2, "partial", *D, next(k), amount="99,00")
            raise AssertionError("8: a partial above the total must fail")
        except LedgerError:
            pass
        assert summary(conn, u2)["state"] == "unknown", "8: a failed reconciliation rolls back"

        # 9. P07.T3 - two threads, same key, one payment
        d = visit(pid, "2026-09-01", [5000])
        issue(conn, d, *D)
        results = []

        def pay():
            c2 = init_db(db_path)
            try:
                results.append(record_payment(c2, d, "50", "cash", "race-key", *D))
            except Exception as e:
                results.append(e)
            finally:
                c2.close()

        threads = [threading.Thread(target=pay) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert sum(1 for r in results if isinstance(r, tuple) and r[1]) == 1, results
        assert summary(conn, d)["paid_cents"] == 5000, "9: the race must not recount"

        # 10. a failure half way through an allocation leaves nothing behind
        e = visit(pid, "2026-09-02", [5000])
        issue(conn, e, *D)
        before = conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
        conn.execute("CREATE TRIGGER boom BEFORE INSERT ON payment_allocations"
                     " BEGIN SELECT RAISE(ABORT, 'boom'); END")
        try:
            record_payment(conn, e, "10", "cash", next(k), *D)
            raise AssertionError("10: the injected failure should surface")
        except sqlite3.IntegrityError:
            pass
        conn.execute("DROP TRIGGER boom")
        assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == before, \
            "10: a payment without its allocation must roll back"

        # 11. history is append-only, and an assistant or admin cannot change money
        for sql in ("UPDATE payments SET amount_cents = 1", "DELETE FROM payment_allocations"):
            try:
                conn.execute(sql)
                raise AssertionError(f"11: {sql} should be refused")
            except sqlite3.IntegrityError:
                conn.rollback()
        # reception may record a payment since 2026-09-22 (section 15); admin never
        try:
            record_payment(conn, d, "1", "cash", next(k), "x", "admin")
            raise AssertionError("11: admin must not record a payment")
        except PermissionError:
            pass
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'record_payment'"
                            " AND allowed = 0").fetchone()[0] == 1, "11: the refusal is audited"

        # 12. P07.T4 - one patient's summary never includes another's
        mine = {i["id"] for i in patient_summary(conn, pid)["invoices"]}
        theirs = {i["id"] for i in patient_summary(conn, other)["invoices"]}
        assert mine and theirs and not mine & theirs, "12: invoices leak between patients"

        # 14. the allocation rule, approved 2026-09-22: money covers the oldest
        # unpaid installment first. deterministic - ties on a due date go by
        # sequence - and derived from append-only rows, so a refund reopens the
        # most recently covered installment and nothing is rewritten
        f = visit(pid, "2026-09-03", [9000])
        issue(conn, f, *D)
        plan_installments(conn, f, 3, "2026-11-01", *D, every_days=0)
        assert [i["due_date"] for i in summary(conn, f)["installments"]] == ["2026-11-01"] * 3
        p14, _ = record_payment(conn, f, "45,00", "cash", next(k), *D)
        assert [i["open_cents"] for i in summary(conn, f)["installments"]] == [0, 1500, 3000], \
            "14: same due date, so sequence decides"
        refund(conn, p14, "20,00", next(k), *D, "partial refund")
        assert [i["open_cents"] for i in summary(conn, f)["installments"]] == [500, 3000, 3000], \
            "14: a refund reopens the latest covered installment first"
        assert "oldest unpaid installment first" in ALLOCATION_ORDER

        # 15. reception may record a payment and nothing else that changes money
        g2 = visit(pid, "2026-09-04", [4000])
        issue(conn, g2, *D)
        pay_id, made = record_payment(conn, g2, "10,00", "cash", "rec-1", "aassist", "assistant")
        assert made and record_payment(conn, g2, "10,00", "cash", "rec-1", "aassist",
                                       "assistant") == (pay_id, False), "15: idempotent for reception"
        for call in (lambda: refund(conn, pay_id, "1", next(k), "aassist", "assistant", "r"),
                     lambda: reverse(conn, pay_id, next(k), "aassist", "assistant", "r"),
                     lambda: void(conn, g2, "aassist", "assistant", "r"),
                     lambda: issue(conn, visit(pid, "2026-09-05", [100]), "aassist", "assistant"),
                     lambda: plan_installments(conn, g2, 2, "2026-12-01", "aassist", "assistant"),
                     lambda: reconcile(conn, visit(other, "2025-01-01", [100], legacy=True),
                                       "paid", "aassist", "assistant", next(k))):
            try:
                call()
                raise AssertionError("15: reception must not make this change")
            except PermissionError:
                pass
        assert conn.execute("SELECT recorded_role FROM payments WHERE id = ?",
                            (pay_id,)).fetchone()[0] == "assistant", "15: who recorded it"

        # 13. P07.00 - a re-synced note keeps its line ids; a draft follows the
        # note, an issued invoice's lines do not change under it
        from dental_notes_schema import DentalNote
        from storage import upsert_note_sql
        note = DentalNote(patient_name="Lia Ledger", codice_fiscale="ZZLG800101010101",
                          visit_date="2026-09-10", procedures=["rct 46"],
                          invoices=[{"amount": 340.0, "description": "cura canalare"},
                                    {"amount": 0.1, "description": "a"}],
                          clinical_notes="x")
        upsert_note_sql(note, "zzlg-13.json", conn)
        vid = conn.execute("SELECT id FROM visits WHERE source_path = 'zzlg-13.json'").fetchone()[0]
        ids = [r[0] for r in conn.execute("SELECT id FROM invoices WHERE visit_id = ? ORDER BY"
                                          " line_index", (vid,))]
        upsert_note_sql(note, "zzlg-13.json", conn)
        assert ids == [r[0] for r in conn.execute("SELECT id FROM invoices WHERE visit_id = ?"
                                                  " ORDER BY line_index", (vid,))], "13: ids moved"
        inv = conn.execute("SELECT id, state FROM billing_invoices WHERE visit_id = ?",
                           (vid,)).fetchone()
        assert inv["state"] == "draft" and summary(conn, inv["id"])["total_cents"] == 34010, "13"
        note.invoices = note.invoices[:1]
        upsert_note_sql(note, "zzlg-13.json", conn)
        assert summary(conn, inv["id"])["total_cents"] == 34000, "13: a draft follows the note"
        assert conn.execute("SELECT id FROM invoices WHERE visit_id = ?", (vid,)).fetchone()[0] \
            == ids[0], "13: the kept line keeps its id"
        issue(conn, inv["id"], *D)
        note.invoices[0].amount = 999.0
        try:
            upsert_note_sql(note, "zzlg-13.json", conn)
            raise AssertionError("13: an issued invoice's lines must not change")
        except ValueError:
            pass
        assert summary(conn, inv["id"])["total_cents"] == 34000, "13: still the issued amount"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python ledger.py --selftest")
    sys.exit(1)


if __name__ == "__main__":
    main()
