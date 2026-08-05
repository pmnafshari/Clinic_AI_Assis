import sys
from datetime import datetime, timedelta
from pathlib import Path

from cli_session import verify_credentials
from auth import log_audit
from storage import init_db

LOCKOUT_THRESHOLD = 5
LOCKOUT_COOLDOWN_MINUTES = 15

# chosen default, not a locked requirement - 16-CONTEXT.md leaves the number to
# the planner, so a policy decision only has to land here
MIN_PASSWORD_LENGTH = 8

# scope boundary (Pitfall 2): these lockout columns are only enforced by the
# web login path built this phase. cli_session.py is intentionally untouched
# (D-03), so a web-locked account can still log in via the CLI - deliberate,
# not a bug.


def _is_locked(row, now):
    if row is None or not row["locked_until"]:
        return False
    return now < datetime.fromisoformat(row["locked_until"])


def _register_failure(conn, username, row, now):
    # an unknown username must not create a lockout row
    if row is None:
        return
    # increment in SQL, not python - concurrent failed logins run on separate
    # connections under threaded=True and must not lose counts
    lock_ts = (now + timedelta(minutes=LOCKOUT_COOLDOWN_MINUTES)).isoformat()
    conn.execute(
        "UPDATE users SET failed_attempts = failed_attempts + 1,"
        " locked_until = CASE WHEN failed_attempts + 1 >= ?"
        " THEN ? ELSE locked_until END"
        " WHERE username = ?",
        (LOCKOUT_THRESHOLD, lock_ts, username),
    )
    conn.commit()


def _clear_failures(conn, username):
    conn.execute(
        "UPDATE users SET failed_attempts = 0, locked_until = NULL WHERE username = ?", (username,)
    )
    conn.commit()


def _lockout_row(conn, username):
    return conn.execute(
        "SELECT failed_attempts, locked_until FROM users WHERE username = ?", (username,)
    ).fetchone()


def attempt_login(username, password, conn, now=None):
    if now is None:
        now = datetime.now()

    row = _lockout_row(conn, username)

    if _is_locked(row, now):
        log_audit(conn, username, "unknown", "login", None, allowed=0)
        return None

    role = verify_credentials(username, password, conn)
    if role is None:
        _register_failure(conn, username, row, now)
        log_audit(conn, username, "unknown", "login", None, allowed=0)
        return None

    _clear_failures(conn, username)
    log_audit(conn, username, role, "login", None, allowed=1)
    return role


def verify_current_password(username, password, conn, now=None):
    # proves the caller knows the existing password before a self-service
    # change. deliberately not attempt_login: that audits every path under the
    # sign-in action, which would fill the trail with sign-ins that never
    # happened (D-02). the lockout behaviour is shared, the audit action is not.
    if now is None:
        now = datetime.now()

    row = _lockout_row(conn, username)

    if _is_locked(row, now):
        # no distinct signal for a correct password while locked, same as login -
        # otherwise the form tells an attacker when they have guessed right
        log_audit(conn, username, "unknown", "change_password", username, allowed=0)
        return None

    role = verify_credentials(username, password, conn)
    if role is None:
        _register_failure(conn, username, row, now)
        log_audit(conn, username, "unknown", "change_password", username, allowed=0)
        return None

    # a locked account was already refused above, so this cannot be used to
    # escape a lockout. no audit row on success - the route logs the change
    _clear_failures(conn, username)
    return role


def selftest():
    import tempfile
    from werkzeug.security import generate_password_hash

    with tempfile.TemporaryDirectory() as tmp:
        conn = init_db(str(Path(tmp) / "clinic.sqlite"))

        conn.execute(
            "INSERT INTO users (username, password_hash, role, active) VALUES (?, ?, ?, ?)",
            ("drossi", generate_password_hash("goodpass"), "dentist", 1),
        )
        conn.commit()

        # 1. correct password returns the role and resets failed_attempts
        conn.execute("UPDATE users SET failed_attempts = 2 WHERE username = ?", ("drossi",))
        conn.commit()
        assert attempt_login("drossi", "goodpass", conn) == "dentist", \
            "1: correct password should return the role"
        attempts = conn.execute(
            "SELECT failed_attempts FROM users WHERE username = ?", ("drossi",)
        ).fetchone()["failed_attempts"]
        assert attempts == 0, "1: successful login should reset failed_attempts"

        # 2. wrong password returns None and increments failed_attempts
        assert attempt_login("drossi", "wrongpass", conn) is None, \
            "2: wrong password should return None"
        attempts = conn.execute(
            "SELECT failed_attempts FROM users WHERE username = ?", ("drossi",)
        ).fetchone()["failed_attempts"]
        assert attempts == 1, "2: failed login should increment failed_attempts"

        # 3. 5 consecutive wrong passwords lock the account
        for _ in range(4):
            attempt_login("drossi", "wrongpass", conn)
        row = conn.execute(
            "SELECT failed_attempts, locked_until FROM users WHERE username = ?", ("drossi",)
        ).fetchone()
        assert row["failed_attempts"] == 5, \
            f"3: expected 5 failed attempts, got {row['failed_attempts']}"
        assert row["locked_until"] is not None, "3: account should be locked after 5 failures"
        locked_until = datetime.fromisoformat(row["locked_until"])
        assert locked_until > datetime.now(), "3: locked_until should be in the future"

        # 4. while locked, even the correct password returns None - no distinct signal
        assert attempt_login("drossi", "goodpass", conn) is None, \
            "4: correct password during lockout should still return None"

        # 5. advancing past locked_until lets a correct password succeed again
        after_cooldown = locked_until + timedelta(seconds=1)
        assert attempt_login("drossi", "goodpass", conn, now=after_cooldown) == "dentist", \
            "5: correct password after cooldown should succeed"
        row = conn.execute(
            "SELECT failed_attempts, locked_until FROM users WHERE username = ?", ("drossi",)
        ).fetchone()
        assert row["failed_attempts"] == 0, "5: successful login after cooldown should reset counter"
        assert row["locked_until"] is None, "5: successful login after cooldown should clear lockout"

        # 6. every attempt above wrote an audit_log row with action "login"
        rows = conn.execute(
            "SELECT allowed FROM audit_log WHERE action = 'login' ORDER BY id"
        ).fetchall()
        assert len(rows) == 8, f"6: expected 8 login audit rows, got {len(rows)}"
        allowed_flags = [r["allowed"] for r in rows]
        assert allowed_flags == [1, 0, 0, 0, 0, 0, 0, 1], \
            f"6: unexpected audit allowed sequence {allowed_flags}"

        # 7. the increment is atomic in SQL - a concurrent failed login landing
        # between attempt_login's read and its write must not be lost
        real_verify = globals()["verify_credentials"]

        def racing_verify(u, p, c):
            # simulate a parallel request failing mid-flight
            c.execute(
                "UPDATE users SET failed_attempts = failed_attempts + 1 WHERE username = ?", (u,)
            )
            c.commit()
            return real_verify(u, p, c)

        globals()["verify_credentials"] = racing_verify
        try:
            attempt_login("drossi", "wrongpass", conn)
        finally:
            globals()["verify_credentials"] = real_verify
        attempts = conn.execute(
            "SELECT failed_attempts FROM users WHERE username = ?", ("drossi",)
        ).fetchone()["failed_attempts"]
        assert attempts == 2, \
            f"7: expected both increments to count (2), got {attempts} - lost update"

        def login_rows():
            return conn.execute(
                "SELECT COUNT(*) c FROM audit_log WHERE action = 'login'"
            ).fetchone()["c"]

        def lockout_state():
            return conn.execute(
                "SELECT failed_attempts, locked_until FROM users WHERE username = ?", ("drossi",)
            ).fetchone()

        # 8. a correct current password returns the role and writes no login row.
        # counting rather than hardcoding keeps this independent of sections 1-7,
        # and it is the guard that fails if anyone reroutes this through
        # attempt_login - which is the whole reason the function exists (D-02)
        _clear_failures(conn, "drossi")
        before = login_rows()
        assert verify_current_password("drossi", "goodpass", conn) == "dentist", \
            "8: correct current password should return the role"
        assert login_rows() == before, \
            "8: verify_current_password must not write an audit row with action 'login'"

        # 9. a wrong current password returns None, counts, and audits as change_password
        before = login_rows()
        assert verify_current_password("drossi", "nope", conn) is None, \
            "9: wrong current password should return None"
        assert lockout_state()["failed_attempts"] == 1, \
            "9: wrong current password should increment failed_attempts"
        denied = conn.execute(
            "SELECT COUNT(*) c FROM audit_log WHERE action = 'change_password' AND allowed = 0"
        ).fetchone()["c"]
        assert denied == 1, f"9: expected 1 denied change_password row, got {denied}"
        assert login_rows() == before, "9: a failed check must not write a login row"

        # 10. repeated wrong current passwords lock the account, and the correct
        # password is then refused with no distinct signal (mirrors section 4)
        for _ in range(4):
            verify_current_password("drossi", "nope", conn)
        row = lockout_state()
        assert row["failed_attempts"] == 5, \
            f"10: expected 5 failed attempts, got {row['failed_attempts']}"
        assert row["locked_until"] is not None, "10: account should be locked after 5 failures"
        assert verify_current_password("drossi", "goodpass", conn) is None, \
            "10: correct current password during lockout should still return None"

        # 11. after the cooldown the correct password succeeds and clears the counters
        locked_until = datetime.fromisoformat(lockout_state()["locked_until"])
        after_cooldown = locked_until + timedelta(seconds=1)
        assert verify_current_password("drossi", "goodpass", conn, now=after_cooldown) == "dentist", \
            "11: correct current password after cooldown should succeed"
        row = lockout_state()
        assert row["failed_attempts"] == 0, "11: success should reset failed_attempts"
        assert row["locked_until"] is None, "11: success should clear locked_until"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python web_auth.py --selftest")


if __name__ == "__main__":
    main()
