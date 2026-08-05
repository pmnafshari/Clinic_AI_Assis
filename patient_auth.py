"""patient identity: credentials and sessions for the patient chatbot.

deliberately shares no function with web_auth.py or web_session.py. the
architecture doc (§2.4) names a shared create_session(table_name, ...) as the
exact failure mode the separation rule exists to prevent, so the technique is
copied here and the modules are not imported. auth.log_audit is the one
sanctioned reuse - it is an audit utility, not an auth mechanism.
"""

import hashlib
import secrets
import sqlite3
import sys
from datetime import datetime, timedelta

from werkzeug.security import check_password_hash, generate_password_hash

import storage
from auth import log_audit
from dental_notes_schema import CF_PATTERN

PATIENT_COOKIE_NAME = "patient_session"

# chosen here, not locked. only the staff-vs-patient asymmetry is the locked
# part of the design: patients are more likely on shared or unmanaged devices,
# so 15 against web_session's 30.
PATIENT_IDLE_MINUTES = 15
PIN_LOCKOUT_THRESHOLD = 5
PIN_LOCKOUT_COOLDOWN_MINUTES = 15
PIN_LENGTH = 8
CREDENTIAL_VALIDITY_DAYS = 7

SCHEMA = """
CREATE TABLE IF NOT EXISTS patient_credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    codice_fiscale TEXT NOT NULL UNIQUE REFERENCES patients(codice_fiscale),
    pin_hash TEXT NOT NULL,
    must_change_pin INTEGER NOT NULL DEFAULT 1,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS patient_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT UNIQUE NOT NULL,
    codice_fiscale TEXT NOT NULL REFERENCES patients(codice_fiscale),
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
"""


def init_patient_tables(conn):
    # no role column and no join to users in either table - that absence is a
    # binding security property, not a style choice
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _pin_is_weak(pin):
    if len(set(pin)) == 1:
        return True
    digits = [int(c) for c in pin]
    steps = {b - a for a, b in zip(digits, digits[1:])}
    return steps in ({1}, {-1})


def _generate_pin():
    # digits only - the pin is read aloud over the phone at handover
    for _ in range(100):
        pin = "".join(secrets.choice("0123456789") for _ in range(PIN_LENGTH))
        if not _pin_is_weak(pin):
            return pin
    raise RuntimeError("could not generate a policy-compliant pin")


def _require_cf(cf):
    if not CF_PATTERN.match(cf or ""):
        raise ValueError(f"codice_fiscale must match ^[A-Z]{{4}}[0-9]{{12}}$, got {cf!r}")


def _credential(conn, cf):
    return conn.execute(
        "SELECT * FROM patient_credentials WHERE codice_fiscale = ?", (cf,)
    ).fetchone()


def issue_pin(cf, conn, issued_by, issued_by_role="staff", now=None):
    # returns the PLAINTEXT pin. this is the only moment it exists outside
    # memory - the caller must show it once and never persist it. reissue
    # supersedes the previous credential and clears any lockout, which is the
    # only recovery path in the design.
    _require_cf(cf)
    if now is None:
        now = datetime.now()

    pin = _generate_pin()
    expires = now + timedelta(days=CREDENTIAL_VALIDITY_DAYS)
    conn.execute("""
        INSERT INTO patient_credentials
            (codice_fiscale, pin_hash, must_change_pin, issued_at, expires_at,
             failed_attempts, locked_until, active)
        VALUES (?, ?, 1, ?, ?, 0, NULL, 1)
        ON CONFLICT(codice_fiscale) DO UPDATE SET
            pin_hash = excluded.pin_hash,
            must_change_pin = 1,
            issued_at = excluded.issued_at,
            expires_at = excluded.expires_at,
            failed_attempts = 0,
            locked_until = NULL,
            active = 1
    """, (cf, generate_password_hash(pin), now.isoformat(), expires.isoformat()))
    conn.commit()

    # the pin itself is never in the audit row - only that one was issued
    log_audit(conn, issued_by, issued_by_role, "issue_patient_pin", cf, allowed=1)
    return pin


def verify_pin(cf, pin, conn, now=None):
    # returns (status, row) where status is ok / wrong / unknown / expired /
    # locked. the caller MUST render "unknown" and "wrong" identically - this
    # surface is internet-reachable and must not confirm whether a codice
    # fiscale belongs to a patient of this clinic. "expired" and "locked" are
    # distinct on purpose: the patient already proved they hold a real
    # credential, and telling them to contact the clinic is a success criterion.
    if now is None:
        now = datetime.now()
    if not CF_PATTERN.match(cf or ""):
        return "unknown", None

    row = _credential(conn, cf)
    if row is None:
        return "unknown", None
    if row["active"] != 1:
        return "wrong", None

    # expiry only gates the temporary credential; once the patient has chosen
    # their own pin, expires_at stops applying
    if row["must_change_pin"] and datetime.fromisoformat(row["expires_at"]) <= now:
        log_audit(conn, cf, "patient", "patient_pin_check", cf, allowed=0)
        return "expired", None

    if row["locked_until"] and now < datetime.fromisoformat(row["locked_until"]):
        # no distinct signal for a correct pin while locked, so the form cannot
        # be used to confirm a guess
        log_audit(conn, cf, "patient", "patient_pin_check", cf, allowed=0)
        return "locked", None

    if not check_password_hash(row["pin_hash"], pin or ""):
        lock_ts = (now + timedelta(minutes=PIN_LOCKOUT_COOLDOWN_MINUTES)).isoformat()
        conn.execute(
            "UPDATE patient_credentials SET failed_attempts = failed_attempts + 1,"
            " locked_until = CASE WHEN failed_attempts + 1 >= ?"
            " THEN ? ELSE locked_until END"
            " WHERE codice_fiscale = ?",
            (PIN_LOCKOUT_THRESHOLD, lock_ts, cf),
        )
        conn.commit()
        log_audit(conn, cf, "patient", "patient_pin_check", cf, allowed=0)
        return "wrong", None

    conn.execute(
        "UPDATE patient_credentials SET failed_attempts = 0, locked_until = NULL"
        " WHERE codice_fiscale = ?", (cf,)
    )
    conn.commit()
    return "ok", _credential(conn, cf)


def change_pin(cf, new_pin, conn, now=None):
    # no current-pin re-auth here, unlike the staff change-password flow: the
    # patient authenticated seconds ago and is being compelled to change, so a
    # re-auth prompt is friction with no gain.
    _require_cf(cf)
    if len(new_pin or "") < PIN_LENGTH:
        raise ValueError(f"pin must be at least {PIN_LENGTH} characters")
    if new_pin.isdigit() and _pin_is_weak(new_pin):
        raise ValueError("pin must not be all one digit or a run of consecutive digits")

    conn.execute(
        "UPDATE patient_credentials SET pin_hash = ?, must_change_pin = 0,"
        " failed_attempts = 0, locked_until = NULL WHERE codice_fiscale = ?",
        (generate_password_hash(new_pin), cf),
    )
    conn.commit()
    log_audit(conn, cf, "patient", "patient_change_pin", cf, allowed=1)


def _hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def create_patient_session(conn, cf, now=None):
    if now is None:
        now = datetime.now()
    token = secrets.token_urlsafe(32)
    ts = now.isoformat()
    conn.execute(
        "INSERT INTO patient_sessions (token_hash, codice_fiscale, created_at, last_seen_at)"
        " VALUES (?, ?, ?, ?)",
        (_hash_token(token), cf, ts, ts),
    )
    conn.commit()
    return token


def load_patient_session(conn, token, now=None):
    if not token:
        return None
    if now is None:
        now = datetime.now()

    token_hash = _hash_token(token)
    row = conn.execute("""
        SELECT s.codice_fiscale, s.last_seen_at, c.must_change_pin
        FROM patient_sessions s
        LEFT JOIN patient_credentials c ON c.codice_fiscale = s.codice_fiscale
        WHERE s.token_hash = ?
    """, (token_hash,)).fetchone()
    if row is None:
        return None

    if now - datetime.fromisoformat(row["last_seen_at"]) > timedelta(minutes=PATIENT_IDLE_MINUTES):
        conn.execute("DELETE FROM patient_sessions WHERE token_hash = ?", (token_hash,))
        conn.commit()
        return None

    conn.execute(
        "UPDATE patient_sessions SET last_seen_at = ? WHERE token_hash = ?",
        (now.isoformat(), token_hash),
    )
    conn.commit()
    return {
        "codice_fiscale": row["codice_fiscale"],
        "must_change_pin": row["must_change_pin"],
    }


def destroy_patient_session(conn, token):
    conn.execute("DELETE FROM patient_sessions WHERE token_hash = ?", (_hash_token(token),))
    conn.commit()


def destroy_patient_sessions(conn, cf):
    cur = conn.execute("DELETE FROM patient_sessions WHERE codice_fiscale = ?", (cf,))
    conn.commit()
    return cur.rowcount


def selftest():
    import tempfile
    from pathlib import Path

    cf = "FRRR850010150200"
    other = "RSSM800010150100"

    with tempfile.TemporaryDirectory() as tmp:
        conn = storage.init_db(str(Path(tmp) / "clinic.sqlite"))
        init_patient_tables(conn)
        for c, name in ((cf, "test patient"), (other, "other patient")):
            conn.execute(
                "INSERT INTO patients (codice_fiscale, patient_name) VALUES (?, ?)", (c, name)
            )
        conn.commit()

        # 1. the foreign key actually bites - proves the pragma reached this
        # connection. a plain sqlite3.connect would accept the orphan row.
        try:
            issue_pin("ZZZZ999999999999", conn, "test-dentist")
            raise AssertionError("1: a credential for a nonexistent patient should be rejected")
        except sqlite3.IntegrityError:
            pass

        # 2. issuance stores only the hash
        pin = issue_pin(cf, conn, "test-dentist", "dentist")
        assert len(pin) == PIN_LENGTH and pin.isdigit(), "2: pin shape"
        row = _credential(conn, cf)
        assert pin not in row["pin_hash"], "2: the plaintext pin must not be in the stored hash"
        assert row["must_change_pin"] == 1, "2: a fresh credential must force a change"
        issued = datetime.fromisoformat(row["issued_at"])
        expires = datetime.fromisoformat(row["expires_at"])
        assert (expires - issued).days == CREDENTIAL_VALIDITY_DAYS, "2: validity window"
        audited = conn.execute(
            "SELECT COUNT(*) c FROM audit_log WHERE action = 'issue_patient_pin' AND target = ?",
            (cf,),
        ).fetchone()["c"]
        assert audited == 1, "2: issuance should be audited"
        leaked = conn.execute(
            "SELECT COUNT(*) c FROM audit_log WHERE target = ?", (pin,)
        ).fetchone()["c"]
        assert leaked == 0, "2: the pin must never appear in the audit log"

        # 3. the five verify outcomes
        assert verify_pin(cf, pin, conn)[0] == "ok", "3: correct pin"
        assert verify_pin(cf, "00000000", conn)[0] == "wrong", "3: wrong pin"
        assert verify_pin(other, pin, conn)[0] == "unknown", "3: no credential row"
        assert verify_pin("not-a-cf", pin, conn)[0] == "unknown", "3: malformed cf"

        conn.execute(
            "UPDATE patient_credentials SET expires_at = ? WHERE codice_fiscale = ?",
            ((datetime.now() - timedelta(days=1)).isoformat(), cf),
        )
        conn.commit()
        assert verify_pin(cf, pin, conn)[0] == "expired", "3: expired temp credential"

        # 4. expiry stops gating once the patient has chosen their own pin
        change_pin(cf, "94620173", conn)
        assert _credential(conn, cf)["must_change_pin"] == 0, "4: flag cleared"
        assert verify_pin(cf, "94620173", conn)[0] == "ok", \
            "4: expires_at must not gate a patient who already changed their pin"

        # 5. lockout, and no distinct signal for a correct pin
        for _ in range(PIN_LOCKOUT_THRESHOLD):
            verify_pin(cf, "00000000", conn)
        locked = _credential(conn, cf)
        assert locked["failed_attempts"] == PIN_LOCKOUT_THRESHOLD, "5: failures counted"
        assert locked["locked_until"] is not None, "5: account should lock"
        assert verify_pin(cf, "94620173", conn)[0] == "locked", \
            "5: a correct pin during lockout must give no distinct signal"

        # 6. reissue supersedes and unlocks
        pin2 = issue_pin(cf, conn, "test-dentist", "dentist")
        assert pin2 != "94620173", "6: reissue should produce a new pin"
        assert verify_pin(cf, "94620173", conn)[0] == "wrong", "6: the old pin must stop working"
        assert verify_pin(cf, pin2, conn)[0] == "ok", "6: the new pin should work"
        fresh = _credential(conn, cf)
        assert fresh["failed_attempts"] == 0 and fresh["locked_until"] is None, \
            "6: reissue must clear the lockout - it is the only recovery path"
        assert conn.execute(
            "SELECT COUNT(*) c FROM patient_credentials WHERE codice_fiscale = ?", (cf,)
        ).fetchone()["c"] == 1, "6: reissue must update in place, not add a row"

        # 7. sessions
        token = create_patient_session(conn, cf)
        loaded = load_patient_session(conn, token)
        assert loaded["codice_fiscale"] == cf, "7: session round-trip"
        assert load_patient_session(conn, "not-a-token") is None, "7: bad token"
        destroy_patient_session(conn, token)
        assert load_patient_session(conn, token) is None, "7: destroyed session"

        token2 = create_patient_session(conn, cf)
        stale = datetime.now() + timedelta(minutes=PATIENT_IDLE_MINUTES + 1)
        assert load_patient_session(conn, token2, now=stale) is None, "7: idle expiry"
        assert conn.execute(
            "SELECT COUNT(*) c FROM patient_sessions WHERE token_hash = ?", (_hash_token(token2),)
        ).fetchone()["c"] == 0, "7: an expired session row should be removed"

        issue_pin(other, conn, "test-dentist", "dentist")
        mine_a = create_patient_session(conn, cf)
        mine_b = create_patient_session(conn, cf)
        theirs = create_patient_session(conn, other)
        assert destroy_patient_sessions(conn, cf) == 2, "7: both of this patient's sessions"
        assert load_patient_session(conn, mine_a) is None and load_patient_session(conn, mine_b) is None
        assert load_patient_session(conn, theirs) is not None, \
            "7: another patient's session must survive"

        # 8. the separation guard, asserted inside the suite so it holds forever.
        # checks the import graph rather than the raw text: naming the staff
        # modules in a comment explaining why they must not be imported is the
        # point, and a substring check would forbid the explanation.
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(sys.modules[__name__]))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for forbidden in ("web_auth", "web_session", "cli_session", "app"):
            assert forbidden not in imported, \
                f"8: patient_auth must not import {forbidden} - binding separation property"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python patient_auth.py --selftest")


if __name__ == "__main__":
    main()
