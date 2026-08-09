"""patient identity: credentials and sessions for the patient chatbot.

deliberately shares no function with web_auth.py or web_session.py. the
architecture doc (§2.4) names a shared create_session(table_name, ...) as the
exact failure mode the separation rule exists to prevent, so the technique is
copied here and the modules are not imported. auth.log_audit is the one
sanctioned reuse - it is an audit utility, not an auth mechanism.
"""

import hashlib
import os
import secrets
import sqlite3
import sys
from datetime import datetime, timedelta

from werkzeug.security import check_password_hash, generate_password_hash

import storage
from auth import log_audit
from dental_notes_schema import CF_PATTERN

PATIENT_COOKIE_NAME = "patient_session"

# chosen here, not locked. defaults secure - the failure mode being avoided
# is shipping a cleartext-eligible bearer token for medical records to the
# most exposed surface in the project. the opt-out is PATIENT_COOKIE_SECURE=0
# for a local http run, and it must stay an opt-out, never the default.
# always-secure was rejected: it would force a TLS setup into a selftest
# suite whose central virtue is running offline with no setup (CR-06, D-13)
COOKIE_SECURE = os.environ.get("PATIENT_COOKIE_SECURE", "1") != "0"

# chosen here, not locked. only the staff-vs-patient asymmetry is the locked
# part of the design: patients are more likely on shared or unmanaged devices,
# so 15 against web_session's 30.
PATIENT_IDLE_MINUTES = 15
PIN_LOCKOUT_THRESHOLD = 5
PIN_LOCKOUT_COOLDOWN_MINUTES = 15
PIN_LENGTH = 8
CREDENTIAL_VALIDITY_DAYS = 7

# chosen here, not locked. the idle window protects an abandoned tab; the
# absolute cap protects a session that is being kept warm by whoever holds
# it, refreshed forever by ordinary use. 12 hours is one clinic day (WR-08).
PATIENT_SESSION_MAX_HOURS = 12

# chosen here, not locked. a sweep needs at least PIN_LOCKOUT_THRESHOLD (5)
# posts per candidate codice fiscale, so 20 caps a single source at roughly
# four candidates per window, while a waiting room or a household behind one
# address still has room to mistype.
PATIENT_IP_ATTEMPT_THRESHOLD = 20
PATIENT_IP_ATTEMPT_WINDOW_MINUTES = 15

# exists so the miss paths in verify_pin pay the same key derivation as the
# hit path (D-03) - costs one scrypt run at import, nothing more
_DUMMY_HASH = generate_password_hash("0" * PIN_LENGTH)

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

CREATE TABLE IF NOT EXISTS patient_login_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ip TEXT NOT NULL,
    attempted_at TEXT NOT NULL
);
"""


def init_patient_tables(conn):
    # no role column and no join to users in either table - that absence is a
    # binding security property, not a style choice
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _pin_is_weak(pin):
    # safe for any string, not just digits - the consecutive-digit run only
    # means something for an all-digit pin, but "all one character" is a weak
    # choice either way (WR-04's isdigit() guard used to skip aaaaaaaa entirely)
    if len(set(pin)) == 1:
        return True
    if not pin.isdigit():
        return False
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


def _record_login_attempt(conn, ip, now):
    conn.execute(
        "INSERT INTO patient_login_attempts (ip, attempted_at) VALUES (?, ?)",
        (ip, now.isoformat()),
    )
    conn.commit()


def _ip_throttled(conn, ip, now):
    # sweep on the request that touches it, same inline shape
    # load_patient_session uses for expired sessions - this project has no
    # scheduler, so there is no reaper to do it separately
    cutoff = (now - timedelta(minutes=PATIENT_IP_ATTEMPT_WINDOW_MINUTES)).isoformat()
    conn.execute("DELETE FROM patient_login_attempts WHERE attempted_at < ?", (cutoff,))
    conn.commit()
    count = conn.execute(
        "SELECT COUNT(*) c FROM patient_login_attempts WHERE ip = ?", (ip,)
    ).fetchone()["c"]
    return count >= PATIENT_IP_ATTEMPT_THRESHOLD


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

    # reissue supersedes the previous credential, so every session standing
    # on it goes too - without this the documented recovery path recovers
    # nothing while a compromised session slides its own idle window forward
    # on every request (D-08)
    destroy_patient_sessions(conn, cf)
    return pin


def verify_pin(cf, pin, conn, now=None, ip=None):
    # returns (status, row) where status is ok / wrong / unknown / expired /
    # locked / throttled. the caller MUST render "unknown" and "wrong"
    # identically - this surface is internet-reachable and must not confirm
    # whether a codice fiscale belongs to a patient of this clinic.
    # "expired" and "locked" are reachable only once the submitted pin is
    # provably correct - that is what closes the enrolment oracle (D-01).
    # they stay distinct once reached: the patient has proved they hold a
    # real credential, and telling them to contact the clinic is a success
    # criterion.
    if now is None:
        now = datetime.now()
    # this is what the socket reports and nothing more. it is attacker-controlled
    # at the network layer, and once cloudflare fronts this app it becomes the
    # tunnel's address unless a forwarded-for header is honoured - which carries
    # its own spoofing trade-off and is a phase 20 decision. so treat it as
    # evidence, never as a control input: the per-codice-fiscale lockout below is
    # an independent second brake, and a forged or rotated source still cannot
    # brute-force one patient (D-04).
    # a request with no reported address still buckets into "unknown" rather
    # than bypassing the throttle entirely
    source = ip or "unknown"

    def _refuse(status):
        _record_login_attempt(conn, source, now)
        log_audit(conn, cf, "patient", "patient_pin_check", cf, allowed=0, ip=source)
        return status, None

    if _ip_throttled(conn, source, now):
        # a throttled source must not be able to keep driving other
        # patients' lockout counters - no _record_login_attempt, no
        # credential row touched
        log_audit(conn, cf, "patient", "patient_pin_check", cf, allowed=0, ip=source)
        return "throttled", None

    if not CF_PATTERN.match(cf or ""):
        check_password_hash(_DUMMY_HASH, pin or "")  # equalise the miss-path timing (D-03)
        return _refuse("unknown")

    row = _credential(conn, cf)
    if row is None:
        check_password_hash(_DUMMY_HASH, pin or "")  # equalise the miss-path timing (D-03)
        return _refuse("unknown")

    # WR-01: a cooldown that has already passed must not carry its counter
    # into this attempt
    if row["locked_until"] and now >= datetime.fromisoformat(row["locked_until"]):
        conn.execute(
            "UPDATE patient_credentials SET failed_attempts = 0, locked_until = NULL"
            " WHERE codice_fiscale = ?", (cf,)
        )
        conn.commit()
        row = _credential(conn, cf)

    pin_ok = check_password_hash(row["pin_hash"], pin or "")

    if not pin_ok or row["active"] != 1:
        # a revoked credential presented with the right pin must not read
        # differently from a wrong pin, and it must count toward the
        # lockout and write an audit row like every neighbouring path
        # (D-11, WR-10) - so it folds into this branch rather than
        # returning early
        lock_ts = (now + timedelta(minutes=PIN_LOCKOUT_COOLDOWN_MINUTES)).isoformat()
        conn.execute(
            "UPDATE patient_credentials SET failed_attempts = failed_attempts + 1,"
            " locked_until = CASE WHEN failed_attempts + 1 >= ?"
            " THEN ? ELSE locked_until END"
            " WHERE codice_fiscale = ?",
            (PIN_LOCKOUT_THRESHOLD, lock_ts, cf),
        )
        conn.commit()
        return _refuse("wrong")

    # reachable only once the submitted pin is provably correct - that is
    # what removes the enrolment oracle. the locked branch still creates no
    # session (the 17-01 property).
    if row["must_change_pin"] and datetime.fromisoformat(row["expires_at"]) <= now:
        return _refuse("expired")
    if row["locked_until"] and now < datetime.fromisoformat(row["locked_until"]):
        return _refuse("locked")

    # a successful login records no attempt row - a busy clinic behind one
    # address must not throttle itself
    conn.execute(
        "UPDATE patient_credentials SET failed_attempts = 0, locked_until = NULL"
        " WHERE codice_fiscale = ?", (cf,)
    )
    conn.commit()
    return "ok", _credential(conn, cf)


def change_pin(cf, new_pin, conn, now=None, current_pin=None):
    # cheap checks first, credential check last - a typo in an unrelated
    # field should not be evaluated against the credential (D-07)
    _require_cf(cf)
    row = _credential(conn, cf)
    if row is None:
        raise ValueError("unknown")
    if len(new_pin or "") < PIN_LENGTH:
        raise ValueError("short")
    if _pin_is_weak(new_pin):
        raise ValueError("weak")
    if check_password_hash(row["pin_hash"], new_pin):
        # stops the patient re-entering the temporary pin the clinic just
        # read to them over the phone, which would defeat the whole point
        # of the forced change
        raise ValueError("same")

    if row["must_change_pin"]:
        # the compelled path (D-07): the patient authenticated seconds ago
        # with the temporary pin and is being compelled to change it, so
        # asking again is friction with no gain. current_pin is ignored.
        pass
    else:
        # the voluntary path (D-07/CR-01): /change-pin stays registered for
        # the life of a session, so without this a stolen cookie converts
        # into a permanent account takeover. deliberately does not count a
        # wrong current_pin toward the lockout - see the comment below.
        if not check_password_hash(row["pin_hash"], current_pin or ""):
            raise ValueError("current")

    conn.execute(
        "UPDATE patient_credentials SET pin_hash = ?, must_change_pin = 0,"
        " failed_attempts = 0, locked_until = NULL WHERE codice_fiscale = ?",
        (generate_password_hash(new_pin), cf),
    )
    conn.commit()
    log_audit(conn, cf, "patient", "patient_change_pin", cf, allowed=1)

    # every session for this codice fiscale, including the one making the
    # change - the route's job is to hand that session a fresh token (D-09),
    # not this function's (D-08). a failed current_pin above never reaches
    # here, so a cookie thief who cannot supply it cannot lock the real
    # patient out from inside a session they do not own (T-17-48)
    destroy_patient_sessions(conn, cf)


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
    # join credentials so a revoked pin also kills its live sessions, and use
    # an inner join so a session with no credential row cannot arrive here
    # carrying must_change_pin=None - require_patient_session reads that as
    # falsy and would let it slide past the forced-change gate (D-10, WR-07)
    row = conn.execute("""
        SELECT s.codice_fiscale, s.created_at, s.last_seen_at, c.must_change_pin
        FROM patient_sessions s
        JOIN patient_credentials c ON c.codice_fiscale = s.codice_fiscale
        WHERE s.token_hash = ? AND c.active = 1
    """, (token_hash,)).fetchone()
    if row is None:
        return None

    # checked before the idle window, so a session already past the absolute
    # cap is never touched by the last_seen_at UPDATE below (WR-08)
    if now - datetime.fromisoformat(row["created_at"]) > timedelta(hours=PATIENT_SESSION_MAX_HOURS):
        conn.execute("DELETE FROM patient_sessions WHERE token_hash = ?", (token_hash,))
        conn.commit()
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

        # 9. patient_login_attempts: a durable per-IP throttle, swept on write
        # instead of a reaper (D-04, D-05). fails until the table and the two
        # helpers exist.
        attempt_columns = {row["name"] for row in conn.execute("PRAGMA table_info(patient_login_attempts)")}
        assert attempt_columns == {"id", "ip", "attempted_at"}, \
            f"9: unexpected patient_login_attempts columns, got {attempt_columns}"

        ip_a = "203.0.113.9"
        ip_b = "203.0.113.10"
        now9 = datetime.now()
        assert _ip_throttled(conn, ip_a, now9) is False, "9: fresh table should not throttle"

        for _ in range(PATIENT_IP_ATTEMPT_THRESHOLD):
            _record_login_attempt(conn, ip_a, now9)
        assert _ip_throttled(conn, ip_a, now9) is True, "9: threshold attempts should throttle"
        assert _ip_throttled(conn, ip_b, now9) is False, \
            "9: throttle must be per-source, not global"

        conn.execute("DELETE FROM patient_login_attempts")
        conn.commit()
        for _ in range(PATIENT_IP_ATTEMPT_THRESHOLD):
            _record_login_attempt(conn, ip_a, now9)
        later9 = now9 + timedelta(minutes=PATIENT_IP_ATTEMPT_WINDOW_MINUTES + 1)
        assert _ip_throttled(conn, ip_a, later9) is False, \
            "9: an attempt outside the window must not count"
        remaining9 = conn.execute("SELECT COUNT(*) c FROM patient_login_attempts").fetchone()["c"]
        assert remaining9 == 0, "9: the throttle check should sweep stale rows, not just skip them"

        # 9b. the store is SQLite, not an in-memory dict - attempts recorded
        # on one connection must still be there, and still throttle, once
        # that connection is closed and a new one opens the same db file
        # (D-04, D-05: "survives a process restart")
        db_path9 = str(Path(tmp) / "clinic.sqlite")
        writer9 = storage.connect(db_path9)
        writer9.execute("DELETE FROM patient_login_attempts")
        writer9.commit()
        for _ in range(PATIENT_IP_ATTEMPT_THRESHOLD):
            _record_login_attempt(writer9, ip_a, now9)
        writer9.close()

        reopened9 = storage.connect(db_path9)
        assert _ip_throttled(reopened9, ip_a, now9) is True, \
            "9b: attempts recorded before closing the connection must still throttle after reopening the db"
        reopened9.execute("DELETE FROM patient_login_attempts")
        reopened9.commit()
        reopened9.close()

        # 10. the oracle is closed (D-01). a locked account probed with the
        # wrong pin must read exactly like an unknown codice fiscale, and the
        # 17-01 property survives: a correct pin during lockout still returns
        # locked. fails until verify_pin checks the pin before revealing
        # locked/expired.
        cf10 = "BSSN930010150100"
        conn.execute(
            "INSERT INTO patients (codice_fiscale, patient_name) VALUES (?, ?)",
            (cf10, "oracle patient"),
        )
        conn.commit()
        pin10 = issue_pin(cf10, conn, "test-dentist", "dentist")
        for _ in range(PIN_LOCKOUT_THRESHOLD):
            verify_pin(cf10, "00000000", conn)
        assert _credential(conn, cf10)["locked_until"] is not None, \
            "10: setup - the account should be locked"
        assert verify_pin(cf10, "wrongpin", conn)[0] == "wrong", \
            "10: a locked account probed with the wrong pin must read as wrong, not locked"
        assert verify_pin(cf10, pin10, conn)[0] == "locked", \
            "10: a correct pin during lockout must still return locked - the 17-01 property"

        cf10b = "GNTL940010150200"
        conn.execute(
            "INSERT INTO patients (codice_fiscale, patient_name) VALUES (?, ?)",
            (cf10b, "expired patient"),
        )
        conn.commit()
        pin10b = issue_pin(cf10b, conn, "test-dentist", "dentist")
        conn.execute(
            "UPDATE patient_credentials SET expires_at = ? WHERE codice_fiscale = ?",
            ((datetime.now() - timedelta(days=1)).isoformat(), cf10b),
        )
        conn.commit()
        assert verify_pin(cf10b, "wrongpin", conn)[0] == "wrong", \
            "10: an expired credential probed with the wrong pin must read as wrong, not expired"
        assert verify_pin(cf10b, pin10b, conn)[0] == "expired", \
            "10: the correct pin on an expired credential should still reveal expired"

        # 11. timing is equalised (D-03). the miss path must pay the same key
        # derivation as the hit path - a ratio, not an absolute, since
        # machines differ. fails until _DUMMY_HASH exists and is burned on
        # every miss path.
        import time

        assert "_DUMMY_HASH" in globals(), "11: expected a module-level _DUMMY_HASH"

        cf11 = "PPRT950010150300"
        conn.execute(
            "INSERT INTO patients (codice_fiscale, patient_name) VALUES (?, ?)",
            (cf11, "timing patient"),
        )
        conn.commit()
        issue_pin(cf11, conn, "test-dentist", "dentist")

        # medians of five, not single samples. the known path also writes the
        # failed_attempts row and commits, so one slow disk write on a loaded
        # machine used to drag the ratio under the bar and redden the suite
        # for a property that had not actually broken.
        def _median_elapsed(cf_arg):
            runs = []
            for _ in range(5):
                t0 = time.perf_counter()
                verify_pin(cf_arg, "00000000", conn)
                runs.append(time.perf_counter() - t0)
            runs.sort()
            return runs[2]

        known_elapsed = _median_elapsed(cf11)
        miss_elapsed = _median_elapsed("ZZZZ999999999999")

        # ten timing calls all report the same source, so they eat the per-ip
        # budget the later sections rely on. this section is not testing the
        # throttle - clear what it spent.
        conn.execute("DELETE FROM patient_login_attempts")
        conn.commit()

        assert miss_elapsed >= known_elapsed * 0.25, \
            f"11: miss path ({miss_elapsed:.4f}s) should cost at least 25% of the " \
            f"known path ({known_elapsed:.4f}s)"

        # 12. WR-01 - a cooldown that has already passed must not carry its
        # counter into the next attempt. fails until verify_pin resets
        # failed_attempts/locked_until before evaluating the pin.
        cf12 = "VNZL960010150400"
        conn.execute(
            "INSERT INTO patients (codice_fiscale, patient_name) VALUES (?, ?)",
            (cf12, "cooldown patient"),
        )
        conn.commit()
        pin12 = issue_pin(cf12, conn, "test-dentist", "dentist")
        for _ in range(PIN_LOCKOUT_THRESHOLD):
            verify_pin(cf12, "00000000", conn)
        locked12 = _credential(conn, cf12)
        assert locked12["locked_until"] is not None, "12: setup - the account should be locked"
        after_cooldown = datetime.fromisoformat(locked12["locked_until"]) + timedelta(minutes=1)

        status12, _ = verify_pin(cf12, "wrongpin", conn, now=after_cooldown)
        assert status12 == "wrong", "12: a typo right after the cooldown should read as wrong"
        reset12 = _credential(conn, cf12)
        assert reset12["failed_attempts"] == 1, \
            f"12: failed_attempts should reset to 1 after the cooldown, got {reset12['failed_attempts']}"
        assert reset12["locked_until"] is None, \
            "12: a single typo after the cooldown must not re-lock the account"

        status12b, _ = verify_pin(cf12, pin12, conn, now=after_cooldown)
        assert status12b == "ok", "12: the correct pin at that same moment should still succeed"

        # 13. D-04 - the per-ip throttle sits in front of the per-credential
        # logic. fails until verify_pin accepts ip= and checks _ip_throttled
        # before touching any credential row.
        conn.execute("DELETE FROM patient_login_attempts")
        conn.commit()
        throttle_ip = "203.0.113.9"
        other_ip = "203.0.113.10"
        throttle_cfs = []
        for i in range(PATIENT_IP_ATTEMPT_THRESHOLD):
            tcf = f"THRT{i:012d}"
            conn.execute(
                "INSERT INTO patients (codice_fiscale, patient_name) VALUES (?, ?)",
                (tcf, "throttle patient"),
            )
            conn.commit()
            issue_pin(tcf, conn, "test-dentist", "dentist")
            throttle_cfs.append(tcf)
            verify_pin(tcf, "00000000", conn, ip=throttle_ip)

        target_cf13 = throttle_cfs[0]
        before_attempts13 = _credential(conn, target_cf13)["failed_attempts"]
        status13, _ = verify_pin(target_cf13, "00000000", conn, ip=throttle_ip)
        assert status13 == "throttled", "13: the next call from a throttled ip should return throttled"
        after_attempts13 = _credential(conn, target_cf13)["failed_attempts"]
        assert after_attempts13 == before_attempts13, \
            "13: a throttled attempt must not still increment the targeted credential's failed_attempts"

        status13b, _ = verify_pin(target_cf13, "00000000", conn, ip=other_ip)
        assert status13b != "throttled", "13: a different source ip must not be throttled by another ip's sweep"

        # 14. D-06 - every failed verify_pin call, including an unknown
        # codice fiscale, writes a patient_pin_check row carrying the source
        # ip. fails until the ip flows into log_audit and the unknown/
        # malformed paths audit too.
        conn.execute("DELETE FROM patient_login_attempts")
        conn.commit()
        cf14 = "GRSS970010150500"
        conn.execute(
            "INSERT INTO patients (codice_fiscale, patient_name) VALUES (?, ?)",
            (cf14, "audit patient"),
        )
        conn.commit()
        issue_pin(cf14, conn, "test-dentist", "dentist")

        before_rows14 = conn.execute(
            "SELECT COUNT(*) c FROM audit_log WHERE action = 'patient_pin_check'"
        ).fetchone()["c"]
        verify_pin(cf14, "00000000", conn, ip="203.0.113.9")
        newest14 = conn.execute(
            "SELECT ip FROM audit_log WHERE action = 'patient_pin_check' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert newest14["ip"] == "203.0.113.9", \
            f"14: the newest patient_pin_check row should carry the source ip, got " \
            f"{dict(newest14) if newest14 else None}"
        after_rows14 = conn.execute(
            "SELECT COUNT(*) c FROM audit_log WHERE action = 'patient_pin_check'"
        ).fetchone()["c"]
        assert after_rows14 == before_rows14 + 1, "14: a failed attempt should write exactly one audit row"

        before_unknown14 = after_rows14
        verify_pin("ZZZZ777777777777", "00000000", conn, ip="203.0.113.11")
        after_unknown14 = conn.execute(
            "SELECT COUNT(*) c FROM audit_log WHERE action = 'patient_pin_check'"
        ).fetchone()["c"]
        assert after_unknown14 == before_unknown14 + 1, \
            "14: an unknown codice fiscale must also write a patient_pin_check row - today it produces none"

        # 15. reissue destroys every session for that patient, on the
        # production path (D-08). fails until issue_pin calls
        # destroy_patient_sessions.
        cf15 = "MRNZ980010151100"
        other15 = "TRNL990010151200"
        conn.execute(
            "INSERT INTO patients (codice_fiscale, patient_name) VALUES (?, ?)",
            (cf15, "reissue patient"),
        )
        conn.execute(
            "INSERT INTO patients (codice_fiscale, patient_name) VALUES (?, ?)",
            (other15, "other reissue patient"),
        )
        conn.commit()
        issue_pin(cf15, conn, "test-dentist", "dentist")
        issue_pin(other15, conn, "test-dentist", "dentist")
        sess15a = create_patient_session(conn, cf15)
        sess15b = create_patient_session(conn, cf15)
        other_sess15 = create_patient_session(conn, other15)

        issue_pin(cf15, conn, "test-dentist", "dentist")

        assert load_patient_session(conn, sess15a) is None, \
            "15: reissue must destroy this patient's existing sessions"
        assert load_patient_session(conn, sess15b) is None, \
            "15: reissue must destroy every one of this patient's sessions, not just one"
        assert load_patient_session(conn, other_sess15) is not None, \
            "15: reissue for one patient must not touch another patient's session"
        assert conn.execute(
            "SELECT COUNT(*) c FROM patient_sessions WHERE codice_fiscale = ?", (cf15,)
        ).fetchone()["c"] == 0, "15: no session rows should remain for the reissued patient"

        # 16. a revoked credential (active=0) kills its live sessions (D-10 /
        # CR-02). only the revoked-state assertion is pinned - whether the
        # session row is later deleted or just fails the join once
        # reactivated is not over-specified here. fails until
        # load_patient_session inner-joins on active=1.
        cf16 = "LNDR900010151300"
        conn.execute(
            "INSERT INTO patients (codice_fiscale, patient_name) VALUES (?, ?)",
            (cf16, "revoked patient"),
        )
        conn.commit()
        issue_pin(cf16, conn, "test-dentist", "dentist")
        sess16 = create_patient_session(conn, cf16)
        assert load_patient_session(conn, sess16) is not None, "16: setup - session should load"

        conn.execute("UPDATE patient_credentials SET active = 0 WHERE codice_fiscale = ?", (cf16,))
        conn.commit()
        assert load_patient_session(conn, sess16) is None, \
            "16: a session must not load while its credential is revoked"

        conn.execute("UPDATE patient_credentials SET active = 1 WHERE codice_fiscale = ?", (cf16,))
        conn.commit()

        # 17. an orphan session (a patients row exists, but no credential row
        # at all) must not load - it must not return a dict carrying
        # must_change_pin=None, which require_patient_session reads as
        # falsy (WR-07). fails until the outer join becomes an inner one.
        cf17 = "PZZL910010151400"
        conn.execute(
            "INSERT INTO patients (codice_fiscale, patient_name) VALUES (?, ?)",
            (cf17, "orphan patient"),
        )
        conn.commit()
        now17 = datetime.now()
        token17 = "orphan-token-17"
        conn.execute(
            "INSERT INTO patient_sessions (token_hash, codice_fiscale, created_at, last_seen_at)"
            " VALUES (?, ?, ?, ?)",
            (_hash_token(token17), cf17, now17.isoformat(), now17.isoformat()),
        )
        conn.commit()
        assert load_patient_session(conn, token17) is None, \
            "17: a session with no credential row must not load at all"

        # 18. a session cannot outlive an absolute cap no matter how often
        # last_seen_at is refreshed (WR-08). fails until
        # PATIENT_SESSION_MAX_HOURS exists and is checked against created_at.
        cf18 = "GRVN920010151500"
        conn.execute(
            "INSERT INTO patients (codice_fiscale, patient_name) VALUES (?, ?)",
            (cf18, "lifetime patient"),
        )
        conn.commit()
        issue_pin(cf18, conn, "test-dentist", "dentist")
        token18 = create_patient_session(conn, cf18)

        assert "PATIENT_SESSION_MAX_HOURS" in globals(), \
            "18: expected a module-level PATIENT_SESSION_MAX_HOURS"

        past_cap = datetime.now() + timedelta(hours=PATIENT_SESSION_MAX_HOURS, minutes=1)
        conn.execute(
            "UPDATE patient_sessions SET last_seen_at = ? WHERE token_hash = ?",
            (past_cap.isoformat(), _hash_token(token18)),
        )
        conn.commit()
        assert load_patient_session(conn, token18, now=past_cap) is None, \
            "18: a session past the absolute cap must not load even with a fresh last_seen_at"
        assert conn.execute(
            "SELECT COUNT(*) c FROM patient_sessions WHERE token_hash = ?", (_hash_token(token18),)
        ).fetchone()["c"] == 0, "18: a session past the absolute cap should be deleted"

        token18b = create_patient_session(conn, cf18)
        inside_cap = datetime.now() + timedelta(hours=PATIENT_SESSION_MAX_HOURS - 1)
        # refresh last_seen_at to the same moment, so this checks only the
        # absolute cap - not the unrelated 15-minute idle window
        conn.execute(
            "UPDATE patient_sessions SET last_seen_at = ? WHERE token_hash = ?",
            (inside_cap.isoformat(), _hash_token(token18b)),
        )
        conn.commit()
        assert load_patient_session(conn, token18b, now=inside_cap) is not None, \
            "18: a session just inside the cap should still load"

        # 19. D-07 - the compelled first change stays exempt: no current_pin
        # is required while must_change_pin is still set. pinned so a later
        # tightening cannot silently remove the exemption.
        cf19 = "FRCD940010151600"
        conn.execute(
            "INSERT INTO patients (codice_fiscale, patient_name) VALUES (?, ?)",
            (cf19, "forced change patient"),
        )
        conn.commit()
        issue_pin(cf19, conn, "test-dentist", "dentist")
        assert _credential(conn, cf19)["must_change_pin"] == 1, \
            "19: setup - a fresh credential forces a change"
        change_pin(cf19, "15935728", conn)
        assert _credential(conn, cf19)["must_change_pin"] == 0, \
            "19: the compelled first change must succeed with no current_pin"

        # 20. D-07/CR-01 - once must_change_pin is cleared, a change is
        # voluntary and requires the correct current pin. fails until
        # change_pin accepts and checks a current_pin argument.
        cf20 = "VLNT950010151700"
        conn.execute(
            "INSERT INTO patients (codice_fiscale, patient_name) VALUES (?, ?)",
            (cf20, "voluntary change patient"),
        )
        conn.commit()
        issue_pin(cf20, conn, "test-dentist", "dentist")
        change_pin(cf20, "15935728", conn)  # the compelled change - clears the flag
        before_hash20 = _credential(conn, cf20)["pin_hash"]

        try:
            change_pin(cf20, "26846913", conn)
            raise AssertionError("20: a voluntary change with no current_pin should be refused")
        except ValueError as exc:
            assert str(exc) == "current", f"20: expected ValueError('current'), got {exc!r}"

        try:
            change_pin(cf20, "26846913", conn, current_pin="00000000")
            raise AssertionError("20: a voluntary change with the wrong current_pin should be refused")
        except ValueError as exc:
            assert str(exc) == "current", f"20: expected ValueError('current'), got {exc!r}"

        assert _credential(conn, cf20)["pin_hash"] == before_hash20, \
            "20: a refused voluntary change must not half-write pin_hash"

        change_pin(cf20, "26846913", conn, current_pin="15935728")
        assert verify_pin(cf20, "26846913", conn)[0] == "ok", \
            "20: a voluntary change with the correct current_pin should succeed"

        # 21. WR-04 - three distinguishable refusals, and the isdigit()
        # guard is gone. fails until change_pin raises short/weak/same
        # instead of the two old long sentences, and a non-digit pin is
        # subject to the weakness policy.
        cf21 = "WRFR960010151800"
        conn.execute(
            "INSERT INTO patients (codice_fiscale, patient_name) VALUES (?, ?)",
            (cf21, "wr04 patient"),
        )
        conn.commit()
        pin21 = issue_pin(cf21, conn, "test-dentist", "dentist")

        try:
            change_pin(cf21, "1234567", conn)
            raise AssertionError("21: a 7-character pin should be refused")
        except ValueError as exc:
            assert str(exc) == "short", f"21: expected ValueError('short'), got {exc!r}"

        for weak21 in ("11111111", "aaaaaaaa", "12345678"):
            try:
                change_pin(cf21, weak21, conn)
                raise AssertionError(f"21: {weak21!r} should be refused as weak")
            except ValueError as exc:
                assert str(exc) == "weak", \
                    f"21: expected ValueError('weak') for {weak21!r}, got {exc!r}"

        try:
            change_pin(cf21, pin21, conn)
            raise AssertionError("21: reusing the current pin should be refused")
        except ValueError as exc:
            assert str(exc) == "same", f"21: expected ValueError('same'), got {exc!r}"

        # 22. D-08's change half - a successful change_pin destroys every
        # session for that patient, and spares another patient's sessions.
        # fails until change_pin calls destroy_patient_sessions.
        cf22 = "CHNG970010151900"
        other22 = "OTHR980010152000"
        conn.execute(
            "INSERT INTO patients (codice_fiscale, patient_name) VALUES (?, ?)",
            (cf22, "change session patient"),
        )
        conn.execute(
            "INSERT INTO patients (codice_fiscale, patient_name) VALUES (?, ?)",
            (other22, "other change session patient"),
        )
        conn.commit()
        issue_pin(cf22, conn, "test-dentist", "dentist")
        issue_pin(other22, conn, "test-dentist", "dentist")
        sess22a = create_patient_session(conn, cf22)
        sess22b = create_patient_session(conn, cf22)
        other_sess22 = create_patient_session(conn, other22)

        change_pin(cf22, "37159428", conn)

        assert load_patient_session(conn, sess22a) is None, \
            "22: change_pin must destroy this patient's existing sessions"
        assert load_patient_session(conn, sess22b) is None, \
            "22: change_pin must destroy every one of this patient's sessions"
        assert load_patient_session(conn, other_sess22) is not None, \
            "22: change_pin for one patient must not touch another patient's session"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python patient_auth.py --selftest")


if __name__ == "__main__":
    main()
