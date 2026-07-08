import sys
from pathlib import Path

from auth import authorize, log_audit
from cli_session import read_session
from storage import init_db


def unlock(username, conn):
    cursor = conn.execute(
        "UPDATE users SET failed_attempts = 0, locked_until = NULL WHERE username = ?",
        (username,),
    )
    conn.commit()
    return cursor.rowcount


def run_unlock(username, conn, role, acting_user):
    # unlocking undoes the brute-force protection, so it is gated and audited
    # like every other sensitive action in this repo
    if not authorize(role, "manage_users"):
        log_audit(conn, acting_user, role, "unlock_user", target=username, allowed=0)
        print(f"not permitted: {role} may not unlock accounts")
        return False
    changed = unlock(username, conn)
    log_audit(conn, acting_user, role, "unlock_user", target=username, allowed=1)
    if changed == 0:
        print(f"no account found for {username}")
    else:
        print(f"account unlocked: {username}")
    return True


def selftest():
    import tempfile
    from datetime import datetime, timedelta
    from werkzeug.security import generate_password_hash

    with tempfile.TemporaryDirectory() as tmp:
        conn = init_db(str(Path(tmp) / "clinic.sqlite"))

        conn.execute(
            "INSERT INTO users (username, password_hash, role, active, failed_attempts, locked_until)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("drossi", generate_password_hash("goodpass"), "dentist", 1, 5,
             (datetime.now() + timedelta(minutes=15)).isoformat()),
        )
        conn.commit()

        # 1. unlock clears the counters for an existing locked account
        changed = unlock("drossi", conn)
        assert changed == 1, f"1: expected 1 row changed, got {changed}"
        row = conn.execute(
            "SELECT failed_attempts, locked_until FROM users WHERE username = ?", ("drossi",)
        ).fetchone()
        assert row["failed_attempts"] == 0, "1: failed_attempts should be 0 after unlock"
        assert row["locked_until"] is None, "1: locked_until should be NULL after unlock"

        # 2. unlocking a non-existent username changes 0 rows and does not raise
        changed = unlock("nobody", conn)
        assert changed == 0, f"2: expected 0 rows changed, got {changed}"

        # re-lock the account for the gated-path tests
        lock_ts = (datetime.now() + timedelta(minutes=15)).isoformat()
        conn.execute(
            "UPDATE users SET failed_attempts = 5, locked_until = ? WHERE username = ?",
            (lock_ts, "drossi"),
        )
        conn.commit()

        # 3. a non-admin is denied: lock stays, denied audit row written
        ok = run_unlock("drossi", conn, "dentist", "test-dentist")
        assert ok is False, "3: dentist should be denied unlock"
        row = conn.execute(
            "SELECT failed_attempts, locked_until FROM users WHERE username = ?", ("drossi",)
        ).fetchone()
        assert row["failed_attempts"] == 5, "3: denied unlock must not clear failed_attempts"
        assert row["locked_until"] == lock_ts, "3: denied unlock must not clear locked_until"
        audit = conn.execute(
            "SELECT * FROM audit_log WHERE action = 'unlock_user' ORDER BY id"
        ).fetchall()
        assert len(audit) == 1, f"3: expected 1 unlock audit row, got {len(audit)}"
        assert audit[0]["allowed"] == 0, "3: denied unlock should log allowed=0"
        assert audit[0]["username"] == "test-dentist", "3: audit should carry the acting user"
        assert audit[0]["target"] == "drossi", "3: audit target should be the unlocked account"

        # 4. an admin is allowed: lock cleared, allowed audit row written
        ok = run_unlock("drossi", conn, "admin", "test-admin")
        assert ok is True, "4: admin should be allowed to unlock"
        row = conn.execute(
            "SELECT failed_attempts, locked_until FROM users WHERE username = ?", ("drossi",)
        ).fetchone()
        assert row["failed_attempts"] == 0, "4: allowed unlock should clear failed_attempts"
        assert row["locked_until"] is None, "4: allowed unlock should clear locked_until"
        audit = conn.execute(
            "SELECT * FROM audit_log WHERE action = 'unlock_user' ORDER BY id"
        ).fetchall()
        assert len(audit) == 2, f"4: expected 2 unlock audit rows, got {len(audit)}"
        assert audit[1]["allowed"] == 1, "4: allowed unlock should log allowed=1"
        assert audit[1]["username"] == "test-admin", "4: audit should carry the acting user"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return

    if len(sys.argv) < 2:
        print("usage: python unlock_user.py <username>")
        sys.exit(1)

    session = read_session()
    if session is None:
        print("not logged in - run: python cli_session.py login")
        sys.exit(1)

    username = sys.argv[1]
    Path("db").mkdir(exist_ok=True)
    conn = init_db("db/clinic.sqlite")
    if not run_unlock(username, conn, session["role"], session["username"]):
        sys.exit(1)


if __name__ == "__main__":
    main()
