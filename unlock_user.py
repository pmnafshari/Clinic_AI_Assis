import sys
from pathlib import Path

from storage import init_db


def unlock(username, conn):
    cursor = conn.execute(
        "UPDATE users SET failed_attempts = 0, locked_until = NULL WHERE username = ?",
        (username,),
    )
    conn.commit()
    return cursor.rowcount


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

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return

    if len(sys.argv) < 2:
        print("usage: python unlock_user.py <username>")
        sys.exit(1)

    username = sys.argv[1]
    Path("db").mkdir(exist_ok=True)
    conn = init_db("db/clinic.sqlite")
    changed = unlock(username, conn)
    if changed == 0:
        print(f"no account found for {username}")
    else:
        print(f"account unlocked: {username}")


if __name__ == "__main__":
    main()
