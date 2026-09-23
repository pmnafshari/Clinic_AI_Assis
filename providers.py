"""The provider gate (P11.06). Everything external is off, and stays off.

WHY THIS FILE EXISTS. P07 built a ledger with no payment provider, P09 a
reminder queue with no messaging provider. Both left a seam. This is the gate
in front of those seams, and its whole job is that nothing crosses one by
accident.

THERE IS NO LIVE ADAPTER. The registry holds `disabled` and `sandbox`. Asking
for anything else raises. A live adapter is not a config value that happens to
be turned off - the code does not exist, so no environment variable, no typo
and no leaked secret can start sending or charging. Writing one is a later
phase, behind an approved provider, approved cost and a DPA (D01, D02, D08, all
open).

FOUR THINGS MUST ALL HOLD before anything leaves this machine:
    1. an adapter is configured and is not `disabled`
    2. the feature is enabled (messaging and payments switch independently)
    3. the operator kill switch is off
    4. the spend cap has not been reached
Any one of them blocks, and the refusal is audited with a reason from a closed
list. `allowed()` returns the reason rather than a bare False so the operator
screen and the audit row say the same word.

The sandbox adapter talks to nothing. It has no URL, no credentials and no
network call; it records what it would have done so a test can assert on it.
"""
import hashlib
import hmac
import os
import sqlite3
import sys

import clinic_time
from auth import authorize, log_audit

DISABLED, SANDBOX = "disabled", "sandbox"
KINDS = ("messaging", "payments", "telephony")

# why something did not go out. closed vocabulary: an operator screen and an
# audit row must not disagree, and neither may carry provider text.
BLOCKED_NO_ADAPTER = "no_adapter_configured"
BLOCKED_FEATURE_OFF = "feature_disabled"
BLOCKED_KILL_SWITCH = "kill_switch_on"
BLOCKED_SPEND_CAP = "spend_cap_reached"
REASONS = (BLOCKED_NO_ADAPTER, BLOCKED_FEATURE_OFF, BLOCKED_KILL_SWITCH, BLOCKED_SPEND_CAP)

# the default for both is off. a deployment that sets nothing sends nothing.
ENV_ADAPTER = {"messaging": "CLINIC_MESSAGING_PROVIDER", "payments": "CLINIC_PAYMENT_PROVIDER",
               "telephony": "CLINIC_TELEPHONY_PROVIDER"}
ENV_ENABLED = {"messaging": "CLINIC_MESSAGING_ENABLED", "payments": "CLINIC_PAYMENTS_ENABLED",
               "telephony": "CLINIC_TELEPHONY_ENABLED"}

# a demo ceiling in cents, deliberately small. it is not a budget - D01 is open
# and there is no budget. it is the fourth brake.
DEFAULT_SPEND_CAP_CENTS = 0

SCHEMA = """
    CREATE TABLE IF NOT EXISTS provider_switches (
        kind TEXT PRIMARY KEY CHECK (kind IN ('messaging', 'payments', 'telephony')),
        killed INTEGER NOT NULL DEFAULT 0,
        spend_cap_cents INTEGER NOT NULL DEFAULT 0,
        spent_cents INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT,
        updated_by TEXT
    );
    CREATE TABLE IF NOT EXISTS provider_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        adapter TEXT NOT NULL,
        event TEXT NOT NULL,
        reason TEXT,
        ref TEXT,
        at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_provider_events_at ON provider_events (at DESC);
"""


class ProviderError(Exception):
    """A provider path refused. Never carries provider text."""


class DisabledAdapter:
    name = DISABLED
    live = False
    reason = "no provider is configured (D01, D02, D08 are open)"

    def send(self, *_a, **_k):
        raise ProviderError(BLOCKED_NO_ADAPTER)

    def charge_link(self, *_a, **_k):
        raise ProviderError(BLOCKED_NO_ADAPTER)


class SandboxAdapter:
    """Talks to nothing. No URL, no credentials, no network call.

    It records what it would have done. `accepted` is the most it can ever
    return: accepting a message is not delivering it (R07), and this adapter
    has no channel to hear back from.
    """
    name = SANDBOX
    live = False
    secret = b"sandbox-not-a-secret"

    def __init__(self, fail=None):
        self.sent = []
        self.links = []
        self.fail = list(fail or [])

    def send(self, destination, body, key):
        if self.fail:
            raise self.fail.pop(0)
        self.sent.append({"to": destination, "body": body, "key": key})
        return {"status": "accepted", "provider_ref": f"sbx-{key}"}

    def charge_link(self, invoice_id, amount_cents, currency, expires_at, key):
        if self.fail:
            raise self.fail.pop(0)
        link = {"invoice_id": invoice_id, "amount_cents": amount_cents, "currency": currency,
                "expires_at": expires_at, "key": key,
                "url": f"https://sandbox.invalid/pay/{key}"}
        self.links.append(link)
        return link


REGISTRY = {DISABLED: DisabledAdapter, SANDBOX: SandboxAdapter}


def adapter_for(kind, env=None):
    """-> an adapter instance. Unknown names raise; there is no live entry."""
    if kind not in KINDS:
        raise ValueError(f"unknown provider kind {kind!r}")
    env = os.environ if env is None else env
    name = (env.get(ENV_ADAPTER[kind]) or DISABLED).strip().lower()
    if name not in REGISTRY:
        # a typo, a stale deployment variable, or someone naming a real
        # provider. all three are the same answer: there is no such adapter.
        raise ProviderError(BLOCKED_NO_ADAPTER)
    return REGISTRY[name]()


def feature_enabled(kind, env=None):
    env = os.environ if env is None else env
    return (env.get(ENV_ENABLED[kind]) or "").strip().lower() in ("1", "true", "yes", "on")


def _row(conn, kind):
    row = conn.execute("SELECT * FROM provider_switches WHERE kind = ?", (kind,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO provider_switches (kind, killed, spend_cap_cents, spent_cents)"
                     " VALUES (?, 0, ?, 0)", (kind, DEFAULT_SPEND_CAP_CENTS))
        conn.commit()
        row = conn.execute("SELECT * FROM provider_switches WHERE kind = ?", (kind,)).fetchone()
    return row


def allowed(conn, kind, env=None, cost_cents=0):
    """-> (True, adapter) or (False, reason). The reason is from REASONS."""
    try:
        adapter = adapter_for(kind, env)
    except ProviderError:
        return False, BLOCKED_NO_ADAPTER
    if adapter.name == DISABLED:
        return False, BLOCKED_NO_ADAPTER
    if not feature_enabled(kind, env):
        return False, BLOCKED_FEATURE_OFF
    row = _row(conn, kind)
    if row["killed"]:
        return False, BLOCKED_KILL_SWITCH
    if row["spent_cents"] + cost_cents > row["spend_cap_cents"]:
        return False, BLOCKED_SPEND_CAP
    return True, adapter


def record(conn, kind, adapter, event, reason=None, ref=None, now=None):
    now = now or clinic_time.now_utc()
    conn.execute("INSERT INTO provider_events (kind, adapter, event, reason, ref, at)"
                 " VALUES (?, ?, ?, ?, ?, ?)",
                 (kind, adapter, event, reason, ref, clinic_time.to_storage(now)))
    conn.commit()


def refuse(conn, kind, reason, ref=None, now=None):
    """Record a blocked attempt. Every gate refusal goes through here."""
    assert reason in REASONS, reason
    record(conn, kind, DISABLED, "blocked", reason=reason, ref=ref, now=now)
    return reason


def spend(conn, kind, cents):
    conn.execute("UPDATE provider_switches SET spent_cents = spent_cents + ? WHERE kind = ?",
                 (cents, kind))
    conn.commit()


def set_kill(conn, kind, killed, actor, role, now=None):
    """The operator's stop button. Turning it ON needs no permission beyond
    the surface; turning it OFF is a dentist's, because it re-arms sending."""
    if kind not in KINDS:
        raise ValueError(kind)
    if not killed and not authorize(role, "manage_providers"):
        log_audit(conn, actor, role, "provider_kill_off", kind, allowed=0)
        raise PermissionError(f"{role} may not re-arm {kind}")
    if killed and not authorize(role, "view_providers"):
        log_audit(conn, actor, role, "provider_kill_on", kind, allowed=0)
        raise PermissionError(f"{role} may not touch the provider switches")
    _row(conn, kind)
    now = now or clinic_time.now_utc()
    conn.execute("UPDATE provider_switches SET killed = ?, updated_at = ?, updated_by = ?"
                 " WHERE kind = ?",
                 (1 if killed else 0, clinic_time.to_storage(now), actor, kind))
    conn.commit()
    log_audit(conn, actor, role, "provider_kill_on" if killed else "provider_kill_off",
              kind, allowed=1)
    record(conn, kind, DISABLED, "kill_on" if killed else "kill_off", ref=actor, now=now)
    return True


def status(conn, env=None):
    """What the operator screen shows. No secret, no URL, no provider text."""
    out = []
    for kind in KINDS:
        row = _row(conn, kind)
        try:
            adapter = adapter_for(kind, env).name
        except ProviderError:
            adapter = "unknown (refused)"
        ok, detail = allowed(conn, kind, env)
        out.append({
            "kind": kind, "adapter": adapter, "live": False,
            "feature_enabled": feature_enabled(kind, env),
            "killed": bool(row["killed"]),
            "spend_cap_cents": row["spend_cap_cents"], "spent_cents": row["spent_cents"],
            "can_send": ok, "reason": None if ok else detail,
            "updated_at": row["updated_at"], "updated_by": row["updated_by"],
        })
    return out


def recent_events(conn, limit=50):
    return conn.execute("SELECT * FROM provider_events ORDER BY id DESC LIMIT ?",
                        (limit,)).fetchall()


def sign(payload, secret):
    """The signature a sandbox provider would send. Real providers differ;
    this exists so the verification path has something correct to test against."""
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def verify(payload, signature, secret):
    """Constant-time compare. A wrong signature is not an error to log with
    the value in it - it is a False."""
    if not signature or not isinstance(signature, (str, bytes)):
        # a header that is missing, empty, or not text at all. all three are a
        # failed verification, not an exception for a caller to handle - a
        # signature check that can raise is a signature check that can be
        # skipped by a malformed request.
        return False
    if isinstance(signature, bytes):
        signature = signature.decode("ascii", "ignore")
    try:
        return hmac.compare_digest(sign(payload, secret), signature.strip())
    except (TypeError, ValueError):
        return False


def selftest():
    import tempfile
    from pathlib import Path

    from storage import init_db

    with tempfile.TemporaryDirectory() as tmp:
        conn = init_db(str(Path(tmp) / "clinic.sqlite"))
        D, A = ("drossi", "dentist"), ("aassist", "assistant")
        t0 = clinic_time.read_instant("2026-09-22T08:00:00+00:00")

        # 1. THERE IS NO LIVE ADAPTER. not "configured off" - absent.
        assert set(REGISTRY) == {DISABLED, SANDBOX}, f"1: {set(REGISTRY)}"
        assert all(not REGISTRY[n]().live for n in REGISTRY), "1: nothing here is live"
        for name in ("live", "twilio", "stripe", "production", "real"):
            for kind in KINDS:
                try:
                    adapter_for(kind, {ENV_ADAPTER[kind]: name})
                    raise AssertionError(f"1: {name!r} must not resolve to an adapter")
                except ProviderError:
                    pass

        # 2. nothing configured is the default, in a completely empty env
        for kind in KINDS:
            assert adapter_for(kind, {}).name == DISABLED, "2: the default is disabled"
            assert feature_enabled(kind, {}) is False, "2: the feature defaults off"
            ok, reason = allowed(conn, kind, {})
            assert ok is False and reason == BLOCKED_NO_ADAPTER, f"2: {kind} {reason}"

        # 3. all four brakes, one at a time. each blocks on its own, and the
        # reason it gives is the one the operator screen will show.
        env = {ENV_ADAPTER["messaging"]: SANDBOX}
        ok, reason = allowed(conn, "messaging", env)
        assert not ok and reason == BLOCKED_FEATURE_OFF, f"3: {reason}"
        env[ENV_ENABLED["messaging"]] = "1"
        conn.execute("UPDATE provider_switches SET spend_cap_cents = 100 WHERE kind = 'messaging'")
        conn.commit()
        ok, adapter = allowed(conn, "messaging", env)
        assert ok and adapter.name == SANDBOX, f"3: should be open now: {adapter}"
        set_kill(conn, "messaging", True, *A, now=t0)
        ok, reason = allowed(conn, "messaging", env)
        assert not ok and reason == BLOCKED_KILL_SWITCH, f"3: {reason}"
        set_kill(conn, "messaging", False, *D, now=t0)
        assert allowed(conn, "messaging", env)[0] is True, "3: a dentist re-arms"
        spend(conn, "messaging", 100)
        ok, reason = allowed(conn, "messaging", env)
        assert not ok and reason == BLOCKED_SPEND_CAP, f"3: {reason}"
        assert set(REASONS) >= {BLOCKED_NO_ADAPTER, BLOCKED_FEATURE_OFF,
                                BLOCKED_KILL_SWITCH, BLOCKED_SPEND_CAP}

        # 4. the kill switch is asymmetric on purpose: reception may STOP the
        # outside world without asking, and only a dentist may start it again
        set_kill(conn, "messaging", True, *A, now=t0)
        try:
            set_kill(conn, "messaging", False, *A, now=t0)
            raise AssertionError("4: reception must not re-arm")
        except PermissionError:
            pass
        assert allowed(conn, "messaging", env)[1] == BLOCKED_KILL_SWITCH, "4: still stopped"
        try:
            set_kill(conn, "messaging", True, "anadmin", "admin", now=t0)
            raise AssertionError("4: admin holds neither capability")
        except PermissionError:
            pass
        set_kill(conn, "messaging", False, *D, now=t0)
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action LIKE 'provider_kill%'"
                            " AND allowed = 0").fetchone()[0] == 2, "4: both refusals audited"

        # 5. the sandbox adapter talks to nothing and can only ever ACCEPT.
        # accepted is not delivered (R07) and it has no channel to hear back on.
        sandbox = SandboxAdapter()
        out = sandbox.send("+39 055 1", "promemoria", "k1")
        assert out["status"] == "accepted" and out["provider_ref"] == "sbx-k1"
        assert "delivered" not in str(out), "5: this adapter cannot claim delivery"
        assert sandbox.sent == [{"to": "+39 055 1", "body": "promemoria", "key": "k1"}]
        link = sandbox.charge_link(7, 1000, "EUR", "2026-09-23T08:00:00+00:00", "k2")
        assert link["url"].endswith("k2") and "sandbox.invalid" in link["url"], \
            f"5: a sandbox link points nowhere real: {link['url']}"

        # 6. the operator view carries no secret, no url and no provider text
        conn.execute("UPDATE provider_switches SET spent_cents = 0")
        conn.commit()
        rows = status(conn, env)
        blob = str(rows)
        for leak in ("sandbox-not-a-secret", "https://", "secret", "token", "password"):
            assert leak not in blob, f"6: the operator view leaks {leak!r}"
        assert all(r["live"] is False for r in rows), "6: nothing reports itself live"
        killed_row = [r for r in rows if r["kind"] == "payments"][0]
        assert killed_row["can_send"] is False and killed_row["reason"] == BLOCKED_NO_ADAPTER

        # 7. a refusal is recorded with a closed reason and nothing else
        refuse(conn, "payments", BLOCKED_NO_ADAPTER, ref="invoice:1", now=t0)
        row = recent_events(conn)[0]
        assert row["event"] == "blocked" and row["reason"] == BLOCKED_NO_ADAPTER
        try:
            refuse(conn, "payments", "because the provider said so", now=t0)
            raise AssertionError("7: only closed reasons")
        except AssertionError as e:
            assert "closed" not in str(e), "7: the assert in refuse() is what should fire"

        # 8. signatures: right one verifies, wrong one does not, and neither
        # a missing nor a malformed one raises
        body = b'{"event_id":"e1"}'
        good = sign(body, b"s3cret")
        assert verify(body, good, b"s3cret") is True
        assert verify(body, good, b"other") is False, "8: wrong secret"
        assert verify(b'{"event_id":"e2"}', good, b"s3cret") is False, "8: wrong body"
        for bad in (None, "", "  ", "zz", 12345):
            assert verify(body, bad, b"s3cret") is False, f"8: {bad!r}"
        conn.close()
    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python providers.py --selftest")
    sys.exit(1)


if __name__ == "__main__":
    main()
