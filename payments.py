"""Payment links and the webhook that is the only thing allowed to say "paid".

NOTHING HERE MOVES MONEY. There is no payment provider (D01, D02, D08 all
open), so the only adapter is the sandbox one, which talks to nothing. Real
payments are BLOCKED and the gate in `providers.py` is what blocks them.

THE WEBHOOK IS THE ONLY SOURCE OF A PAYMENT. Not a success URL, not a redirect,
not anything the patient's browser says (R07). `redirect_is_not_proof` exists
to be called by the return route and to record exactly that. A real payment row
is written only by `handle_webhook`, and only after all of this passes:

    signature      HMAC over the raw body, constant-time
    freshness      a timestamp inside a window, so a captured call cannot be replayed
    idempotency    the provider's event id, once, ever
    identity       the invoice exists and the event names it
    currency       matches the invoice's
    amount         a positive integer of minor units, not more than outstanding
    transition     the invoice is in a state that can take money

Any failure refuses with a reason from a closed list, records it, and writes
nothing. `unknown` is a state, never a retry: a timeout means the charge may or
may not have happened, and re-charging is the one thing that must not follow.

CARD DATA NEVER ENTERS. There is no column for it. A payload carrying anything
that looks like one is refused before it is stored anywhere, and a test asserts
both halves.
"""
import hashlib
import json
import re
import sqlite3
import sys
from datetime import timedelta

import clinic_time
import ledger
import providers
from auth import log_audit

KIND = "payments"
FRESHNESS = timedelta(minutes=5)
LINK_TTL = timedelta(hours=24)

# closed vocabulary. never provider text, never the value that failed.
BAD_SIGNATURE = "bad_signature"
STALE = "stale_timestamp"
REPLAY = "duplicate_event"
NO_INVOICE = "unknown_invoice"
BAD_CURRENCY = "currency_mismatch"
BAD_AMOUNT = "amount_invalid"
OVER_OUTSTANDING = "amount_over_outstanding"
BAD_STATE = "invoice_state_refuses_payment"
CARD_DATA = "card_data_present"
MALFORMED = "malformed_payload"
REFUSALS = (BAD_SIGNATURE, STALE, REPLAY, NO_INVOICE, BAD_CURRENCY, BAD_AMOUNT,
            OVER_OUTSTANDING, BAD_STATE, CARD_DATA, MALFORMED)

# a payload that carries any of these is refused outright. the clinic has no
# business receiving them and no place to put them.
CARD_FIELDS = ("pan", "card_number", "cardnumber", "cvv", "cvc", "card_cvv",
               "expiry", "exp_month", "exp_year", "track2", "cardholder")
PAN = re.compile(r"\b(?:\d[ -]?){13,19}\b")

SCHEMA = """
    -- what a link was for. NO card data, no provider secret, no URL with a
    -- token in it once it has expired.
    CREATE TABLE IF NOT EXISTS payment_links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invoice_id INTEGER NOT NULL,
        amount_cents INTEGER NOT NULL,
        currency TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        adapter TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('open', 'paid', 'expired', 'cancelled'))
    );
    -- every webhook seen, accepted or refused, exactly once per event id
    CREATE TABLE IF NOT EXISTS payment_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_event_id TEXT NOT NULL UNIQUE,
        invoice_id INTEGER,
        amount_cents INTEGER,
        currency TEXT,
        outcome TEXT NOT NULL,
        reason TEXT,
        payment_id INTEGER,
        received_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_payment_events_invoice ON payment_events (invoice_id);
"""


class PaymentRefused(Exception):
    def __init__(self, reason):
        assert reason in REFUSALS, reason
        super().__init__(reason)
        self.reason = reason


def _has_card_data(raw, payload):
    if any(field in payload for field in CARD_FIELDS):
        return True
    # a PAN hiding in a value, not a key
    for value in payload.values():
        if isinstance(value, str) and PAN.search(value) and len(re.sub(r"\D", "", value)) >= 13:
            return True
    return False


def create_link(conn, invoice_id, actor, role, env=None, now=None):
    """Ask the adapter for a link. Blocked unless every gate is open."""
    now = now or clinic_time.now_utc()
    ok, detail = providers.allowed(conn, KIND, env)
    if not ok:
        providers.refuse(conn, KIND, detail, ref=f"invoice:{invoice_id}", now=now)
        log_audit(conn, actor, role, "payment_link", f"invoice:{invoice_id}",
                  allowed=0, reason=detail)
        raise providers.ProviderError(detail)
    summary = ledger.summary(conn, invoice_id)
    if summary is None:
        raise PaymentRefused(NO_INVOICE)
    if summary["state"] in ("void", "draft"):
        raise PaymentRefused(BAD_STATE)
    outstanding = summary["outstanding_cents"]
    if outstanding <= 0:
        raise PaymentRefused(BAD_STATE)
    key = hashlib.sha256(
        f"{invoice_id}:{outstanding}:{clinic_time.to_storage(now)}".encode()).hexdigest()[:32]
    expires = now + LINK_TTL
    link = detail.charge_link(invoice_id, outstanding, ledger.CURRENCY,
                              clinic_time.to_storage(expires), key)
    conn.execute(
        "INSERT INTO payment_links (invoice_id, amount_cents, currency, idempotency_key,"
        " adapter, created_at, expires_at, state) VALUES (?, ?, ?, ?, ?, ?, ?, 'open')",
        (invoice_id, outstanding, ledger.CURRENCY, key, detail.name,
         clinic_time.to_storage(now), clinic_time.to_storage(expires)))
    conn.commit()
    providers.record(conn, KIND, detail.name, "link_created", ref=f"invoice:{invoice_id}", now=now)
    log_audit(conn, actor, role, "payment_link", f"invoice:{invoice_id}", allowed=1)
    return link


def redirect_is_not_proof(conn, invoice_id, now=None):
    """The browser came back saying it went well. That is not evidence.

    Called by the return route. It records that the patient was redirected and
    changes nothing else - no payment, no invoice state, no ledger row. Only
    `handle_webhook` may say money arrived (R07).
    """
    providers.record(conn, KIND, providers.DISABLED, "redirect_seen",
                     ref=f"invoice:{invoice_id}", now=now)
    return {"recorded": False, "why": "a browser redirect is not payment evidence"}


def _seen(conn, event_id):
    return conn.execute("SELECT * FROM payment_events WHERE provider_event_id = ?",
                        (event_id,)).fetchone()


def _refuse(conn, event_id, reason, invoice_id=None, now=None):
    now = now or clinic_time.now_utc()
    conn.execute(
        "INSERT OR IGNORE INTO payment_events (provider_event_id, invoice_id, outcome, reason,"
        " received_at) VALUES (?, ?, 'refused', ?, ?)",
        (event_id or f"anon-{clinic_time.to_storage(now)}", invoice_id, reason,
         clinic_time.to_storage(now)))
    conn.commit()
    providers.record(conn, KIND, providers.DISABLED, "webhook_refused", reason=reason,
                     ref=f"invoice:{invoice_id}" if invoice_id else None, now=now)
    raise PaymentRefused(reason)


def handle_webhook(conn, raw_body, signature, secret, now=None,
                   actor="provider-webhook", role="provider"):
    """The only path that can record a payment. Returns (payment_id, created)."""
    now = now or clinic_time.now_utc()

    # 1. the signature, over the raw bytes, before anything is parsed
    if not providers.verify(raw_body, signature, secret):
        _refuse(conn, None, BAD_SIGNATURE, now=now)

    try:
        payload = json.loads(raw_body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError
    except (ValueError, UnicodeDecodeError):
        _refuse(conn, None, MALFORMED, now=now)

    event_id = str(payload.get("event_id") or "").strip()
    if not event_id:
        _refuse(conn, None, MALFORMED, now=now)

    # 2. card data never enters, whatever else is true of this call
    if _has_card_data(raw_body, payload):
        _refuse(conn, event_id, CARD_DATA, now=now)

    # 3. freshness, so a captured call cannot be replayed later
    try:
        sent_at = clinic_time.read_instant(str(payload.get("sent_at")))
    except Exception:
        _refuse(conn, event_id, MALFORMED, now=now)
    if abs((now - sent_at).total_seconds()) > FRESHNESS.total_seconds():
        _refuse(conn, event_id, STALE, now=now)

    # 4. this exact event, once, ever. out-of-order and duplicate both land here
    already = _seen(conn, event_id)
    if already:
        providers.record(conn, KIND, providers.DISABLED, "webhook_duplicate",
                         reason=REPLAY, ref=event_id, now=now)
        return already["payment_id"], False

    invoice_id = payload.get("invoice_id")
    summary = ledger.summary(conn, invoice_id) if isinstance(invoice_id, int) else None
    if summary is None:
        _refuse(conn, event_id, NO_INVOICE, invoice_id=invoice_id, now=now)

    # 5. currency and amount, in minor units, never coerced
    if str(payload.get("currency") or "") != ledger.CURRENCY:
        _refuse(conn, event_id, BAD_CURRENCY, invoice_id=invoice_id, now=now)
    amount = payload.get("amount_cents")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        _refuse(conn, event_id, BAD_AMOUNT, invoice_id=invoice_id, now=now)

    # 6. the invoice must be in a state that can take money
    if summary["state"] in ("void", "draft"):
        _refuse(conn, event_id, BAD_STATE, invoice_id=invoice_id, now=now)
    if amount > summary["outstanding_cents"]:
        _refuse(conn, event_id, OVER_OUTSTANDING, invoice_id=invoice_id, now=now)

    # 7. the ledger is the reference service. the provider's event id is the
    # idempotency key, so the same event twice records one payment even if the
    # row below were somehow missed.
    payment_id, created = ledger.record_payment(
        conn, invoice_id, amount, "card", f"webhook:{event_id}", actor, role,
        reference=event_id, source="provider")
    conn.execute(
        "INSERT INTO payment_events (provider_event_id, invoice_id, amount_cents, currency,"
        " outcome, payment_id, received_at) VALUES (?, ?, ?, ?, 'accepted', ?, ?)",
        (event_id, invoice_id, amount, ledger.CURRENCY, payment_id,
         clinic_time.to_storage(now)))
    conn.execute("UPDATE payment_links SET state = 'paid' WHERE invoice_id = ? AND state = 'open'",
                 (invoice_id,))
    conn.commit()
    providers.record(conn, KIND, providers.SANDBOX, "webhook_accepted",
                     ref=f"invoice:{invoice_id}", now=now)
    return payment_id, created


def selftest():
    import tempfile
    from pathlib import Path

    import patient_id
    from storage import init_db

    def body(now, **over):
        payload = {"event_id": "e-1", "invoice_id": 1, "amount_cents": 1000,
                   "currency": "EUR", "sent_at": clinic_time.to_storage(now)}
        payload.update(over)
        return json.dumps(payload).encode()

    with tempfile.TemporaryDirectory() as tmp:
        conn = init_db(str(Path(tmp) / "clinic.sqlite"))
        D = ("drossi", "dentist")
        secret = b"sandbox-not-a-secret"
        t0 = clinic_time.read_instant("2026-09-22T08:00:00+00:00")
        pid = patient_id.seed_patient(conn, "ZZP00A00A000A", "Paolo Pago", "+39 055 1")
        visit = conn.execute(
            "INSERT INTO visits (patient_id, visit_date, procedures, clinical_notes, source_path)"
            " VALUES (?, '2026-09-01', '[]', '', 'p11/1.json')", (pid,)).lastrowid
        conn.execute("INSERT INTO invoices (patient_id, visit_id, line_index, amount, description)"
                     " VALUES (?, ?, 0, 100.0, 'visita')", (pid, visit))
        conn.commit()
        invoice = ledger.ensure_invoice(conn, pid, visit)
        conn.commit()
        ledger.issue(conn, invoice, *D)
        assert ledger.summary(conn, invoice)["outstanding_cents"] == 10000

        # 1. P11.T1 - with no provider, a link cannot even be asked for
        try:
            create_link(conn, invoice, *D, env={}, now=t0)
            raise AssertionError("1: no provider, no link")
        except providers.ProviderError as e:
            assert str(e) == providers.BLOCKED_NO_ADAPTER, str(e)
        env = {providers.ENV_ADAPTER["payments"]: providers.SANDBOX,
               providers.ENV_ENABLED["payments"]: "1"}
        conn.execute("UPDATE provider_switches SET spend_cap_cents = 100000 WHERE kind='payments'")
        conn.commit()
        link = create_link(conn, invoice, *D, env=env, now=t0)
        assert link["amount_cents"] == 10000 and link["currency"] == "EUR"
        assert link["invoice_id"] == invoice and "sandbox.invalid" in link["url"]

        # 2. THE REDIRECT IS NOT PROOF. the patient's browser says it went
        # well; nothing is recorded and nothing is owed differently.
        before = ledger.summary(conn, invoice)["outstanding_cents"]
        out = redirect_is_not_proof(conn, invoice, now=t0)
        assert out["recorded"] is False
        assert ledger.summary(conn, invoice)["outstanding_cents"] == before, \
            "2: A REDIRECT CHANGED THE LEDGER"
        assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 0

        # 3. a wrong signature creates nothing, and neither does a right
        # signature over a different body
        raw = body(t0)
        for sig in (providers.sign(raw, b"wrong"), providers.sign(body(t0, amount_cents=1), secret),
                    None, "", "garbage"):
            try:
                handle_webhook(conn, raw, sig, secret, now=t0)
                raise AssertionError(f"3: {sig!r} must not be accepted")
            except PaymentRefused as e:
                assert e.reason == BAD_SIGNATURE, e.reason
        assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 0, \
            "3: A BAD SIGNATURE CREATED A PAYMENT"

        # 4. replay: a captured call, valid in every other way, is stale
        old_raw = body(t0 - timedelta(minutes=30))
        try:
            handle_webhook(conn, old_raw, providers.sign(old_raw, secret), secret, now=t0)
            raise AssertionError("4: a 30-minute-old event must be stale")
        except PaymentRefused as e:
            assert e.reason == STALE, e.reason

        # 5. card data never enters, however it is dressed
        for over in ({"pan": "4111111111111111"}, {"cvv": "123"},
                     {"note": "card 4111 1111 1111 1111"}, {"cardholder": "P PAGO"}):
            bad = body(t0, event_id="card-1", **over)
            try:
                handle_webhook(conn, bad, providers.sign(bad, secret), secret, now=t0)
                raise AssertionError(f"5: {list(over)} must be refused")
            except PaymentRefused as e:
                assert e.reason == CARD_DATA, e.reason
        cols = set()
        for table in ("payment_links", "payment_events", "payments"):
            cols |= {r[1].lower() for r in conn.execute(f"PRAGMA table_info({table})")}
        for banned in CARD_FIELDS:
            assert banned not in cols, f"5: a column exists that could hold {banned!r}"

        # 6. identity, currency, amount and state are each checked
        for index, (over, want) in enumerate((({"invoice_id": 9999}, NO_INVOICE),
                           ({"invoice_id": "1"}, NO_INVOICE),
                           ({"currency": "USD"}, BAD_CURRENCY),
                           ({"amount_cents": 0}, BAD_AMOUNT),
                           ({"amount_cents": -500}, BAD_AMOUNT),
                           ({"amount_cents": 10.5}, BAD_AMOUNT),
                           ({"amount_cents": True}, BAD_AMOUNT),
                           ({"amount_cents": 99999}, OVER_OUTSTANDING))):
            # a distinct event id per case: two cases sharing one would make
            # the second land on the idempotency path and look like a pass
            fields = {"invoice_id": invoice, "event_id": f"bad-{index}"}
            fields.update(over)
            bad = body(t0, **fields)
            try:
                handle_webhook(conn, bad, providers.sign(bad, secret), secret, now=t0)
                raise AssertionError(f"6: {over} must be refused")
            except PaymentRefused as e:
                assert e.reason == want, f"6: {over} -> {e.reason}, wanted {want}"
        assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 0, \
            "6: a refused webhook created a payment"

        # 7. a good one is the ONLY thing that records money, and it goes
        # through the ledger - the same service the staff page uses
        good = body(t0, event_id="ok-1", invoice_id=invoice, amount_cents=4000)
        payment_id, created = handle_webhook(conn, good, providers.sign(good, secret), secret,
                                             now=t0)
        assert created and payment_id
        assert ledger.summary(conn, invoice)["outstanding_cents"] == 6000, "7: the ledger moved"
        assert conn.execute("SELECT source FROM payments WHERE id = ?",
                            (payment_id,)).fetchone()[0] == "provider"

        # 8. P11.T2 - the same event twice records one payment, and the second
        # is reported as a duplicate rather than refused
        again, created_again = handle_webhook(conn, good, providers.sign(good, secret), secret,
                                              now=t0)
        assert again == payment_id and created_again is False, "8: duplicate must not double-pay"
        assert ledger.summary(conn, invoice)["outstanding_cents"] == 6000, \
            "8: A DUPLICATE WEBHOOK TOOK THE MONEY TWICE"
        assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 1

        # 9. partial payments accumulate, and one cent over the remainder is
        # refused rather than clamped
        over_raw = body(t0, event_id="ok-2", invoice_id=invoice, amount_cents=6001)
        try:
            handle_webhook(conn, over_raw, providers.sign(over_raw, secret), secret, now=t0)
            raise AssertionError("9: one cent too many must be refused, not clamped")
        except PaymentRefused as e:
            assert e.reason == OVER_OUTSTANDING
        rest = body(t0, event_id="ok-3", invoice_id=invoice, amount_cents=6000)
        handle_webhook(conn, rest, providers.sign(rest, secret), secret, now=t0)
        assert ledger.summary(conn, invoice)["outstanding_cents"] == 0, "9: settled exactly"
        assert conn.execute("SELECT state FROM payment_links WHERE invoice_id = ?",
                            (invoice,)).fetchone()[0] == "paid"

        # 10. every event seen is recorded once, accepted or refused, and the
        # refusal reasons are all from the closed list
        seen = conn.execute("SELECT outcome, reason FROM payment_events").fetchall()
        assert seen, "10: events are recorded"
        for outcome, reason in seen:
            assert outcome in ("accepted", "refused")
            assert reason is None or reason in REFUSALS, f"10: open-ended reason {reason!r}"
        conn.close()

    # 11. the source migration, against a database built with the OLD CHECK.
    # a payment from a verified webhook is not a manual entry; recording it as
    # one would misattribute money. SQLite cannot widen a CHECK in place, so
    # the table is rebuilt - and a rebuild of an append-only money table has to
    # be proven, not assumed.
    import storage
    with tempfile.TemporaryDirectory() as tmp2:
        import os
        os.makedirs(Path(tmp2) / "db")
        db2 = str(Path(tmp2) / "db" / "clinic.sqlite")
        conn = storage.init_db(db2)
        old_ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'payments'"
                               ).fetchone()[0].replace(", 'provider'", "")
        cols = ", ".join(r[1] for r in conn.execute("PRAGMA table_info(payments)"))
        pid2 = patient_id.seed_patient(conn, "ZZM00A00A000A", "Marta Migra", None)
        v2 = conn.execute(
            "INSERT INTO visits (patient_id, visit_date, procedures, clinical_notes, source_path)"
            " VALUES (?, '2026-09-01', '[]', '', 'm/1.json')", (pid2,)).lastrowid
        conn.execute("INSERT INTO invoices (patient_id, visit_id, line_index, amount, description)"
                     " VALUES (?, ?, 0, 50.0, 'v')", (pid2, v2))
        conn.commit()
        inv2 = ledger.ensure_invoice(conn, pid2, v2)
        conn.commit()
        ledger.issue(conn, inv2, *D)
        ledger.record_payment(conn, inv2, 1000, "cash", "k-old", *D)
        # put the database back into its old shape, rows and all. the rename
        # leaves the name QUOTED in sqlite_master, which is the case a plain
        # string replace in the migration silently missed.
        conn.execute("PRAGMA foreign_keys = OFF")
        for trigger in ("payments_no_update", "payments_no_delete"):
            conn.execute(f"DROP TRIGGER {trigger}")
        conn.execute(old_ddl.replace("CREATE TABLE payments", "CREATE TABLE payments_old", 1))
        conn.execute(f"INSERT INTO payments_old ({cols}) SELECT {cols} FROM payments")
        conn.execute("DROP TABLE payments")
        conn.execute("ALTER TABLE payments_old RENAME TO payments")
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
        before_rows = conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
        before_allocs = conn.execute("SELECT COUNT(*) FROM payment_allocations").fetchone()[0]
        ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'payments'").fetchone()[0]
        assert "'provider'" not in ddl and '"payments"' in ddl, "11: old shape, quoted name"
        conn.close()

        conn = storage.init_db(db2)          # the migration runs here
        ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'payments'").fetchone()[0]
        assert "'provider'" in ddl, "11: the constraint was not widened"
        assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == before_rows, \
            "11: THE REBUILD LOST A PAYMENT ROW"
        assert conn.execute("SELECT COUNT(*) FROM payment_allocations").fetchone()[0] \
            == before_allocs, "11: the rebuild lost an allocation"
        assert ledger.summary(conn, inv2)["paid_cents"] == 1000, "11: the ledger still sums"
        triggers = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'payments%'")}
        assert triggers == {"payments_no_update", "payments_no_delete"}, \
            f"11: append-only triggers not restored: {triggers}"
        try:
            conn.execute("UPDATE payments SET amount_cents = 1")
            raise AssertionError("11: APPEND-ONLY WAS LOST IN THE REBUILD")
        except sqlite3.IntegrityError:
            pass
        conn.close()
        conn = storage.init_db(db2)          # and again: a no-op
        assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == before_rows
        conn.close()
    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python payments.py --selftest")
    sys.exit(1)


if __name__ == "__main__":
    main()
