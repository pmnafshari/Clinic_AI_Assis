import sys
from datetime import datetime, timedelta
from pathlib import Path

from cli_session import verify_credentials
from auth import log_audit
from storage import init_db

LOCKOUT_THRESHOLD = 5
LOCKOUT_COOLDOWN_MINUTES = 15

# scope boundary (Pitfall 2): these lockout columns are only enforced by the
# web login path built this phase. cli_session.py is intentionally untouched
# (D-03), so a web-locked account can still log in via the CLI - deliberate,
# not a bug.


def attempt_login(username, password, conn, now=None):
    if now is None:
        now = datetime.now()

    row = conn.execute(
        "SELECT failed_attempts, locked_until FROM users WHERE username = ?", (username,)
    ).fetchone()

    if row is not None and row["locked_until"]:
        locked_until = datetime.fromisoformat(row["locked_until"])
        if now < locked_until:
            log_audit(conn, username, "unknown", "login", None, allowed=0)
            return None

    role = verify_credentials(username, password, conn)
    if role is None:
        if row is not None:
            attempts = row["failed_attempts"] + 1
            locked_until = row["locked_until"]
            if attempts >= LOCKOUT_THRESHOLD:
                locked_until = (now + timedelta(minutes=LOCKOUT_COOLDOWN_MINUTES)).isoformat()
            conn.execute(
                "UPDATE users SET failed_attempts = ?, locked_until = ? WHERE username = ?",
                (attempts, locked_until, username),
            )
            conn.commit()
        log_audit(conn, username, "unknown", "login", None, allowed=0)
        return None

    conn.execute(
        "UPDATE users SET failed_attempts = 0, locked_until = NULL WHERE username = ?", (username,)
    )
    conn.commit()
    log_audit(conn, username, role, "login", None, allowed=1)
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

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python web_auth.py --selftest")


if __name__ == "__main__":
    main()
