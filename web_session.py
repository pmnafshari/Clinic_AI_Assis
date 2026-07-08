import hashlib
import secrets
import sys
from datetime import datetime, timedelta
from pathlib import Path

from storage import init_db

SESSION_IDLE_MINUTES = 30
COOKIE_NAME = "session_token"


def _hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(conn, username, role, now=None):
    if now is None:
        now = datetime.now()

    token = secrets.token_urlsafe(32)
    ts = now.isoformat()
    conn.execute(
        "INSERT INTO sessions (token_hash, username, role, created_at, last_seen_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (_hash_token(token), username, role, ts, ts),
    )
    conn.commit()
    return token


def load_session(conn, token, now=None):
    if now is None:
        now = datetime.now()

    token_hash = _hash_token(token)
    # join users so deactivating an account also kills its live sessions
    row = conn.execute(
        "SELECT s.username, s.role, s.last_seen_at FROM sessions s"
        " JOIN users u ON u.username = s.username"
        " WHERE s.token_hash = ? AND u.active = 1",
        (token_hash,),
    ).fetchone()
    if row is None:
        return None

    last_seen = datetime.fromisoformat(row["last_seen_at"])
    if now - last_seen > timedelta(minutes=SESSION_IDLE_MINUTES):
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
        conn.commit()
        return None

    # sliding refresh - a live session's idle window resets on every read
    conn.execute(
        "UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?",
        (now.isoformat(), token_hash),
    )
    conn.commit()
    return {"username": row["username"], "role": row["role"]}


def destroy_session(conn, token):
    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))
    conn.commit()


def selftest():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        conn = init_db(str(Path(tmp) / "clinic.sqlite"))

        # load_session joins users, so the accounts must exist and be active
        conn.execute(
            "INSERT INTO users (username, password_hash, role, active) VALUES (?, ?, ?, ?)",
            ("drossi", "x", "dentist", 1),
        )
        conn.execute(
            "INSERT INTO users (username, password_hash, role, active) VALUES (?, ?, ?, ?)",
            ("aassist", "x", "assistant", 1),
        )
        conn.commit()

        # 1. create_session returns a raw token that is not stored literally
        token = create_session(conn, "drossi", "dentist")
        assert token, "1: create_session should return a non-empty token"
        row = conn.execute("SELECT token_hash FROM sessions").fetchone()
        assert row["token_hash"] != token, "1: raw token must not be stored in the sessions table"

        # 2. load_session round-trips username/role for a live token
        session = load_session(conn, token)
        assert session == {"username": "drossi", "role": "dentist"}, \
            "2: load_session did not round-trip username/role"

        # 3. an unknown token returns None
        assert load_session(conn, "not-a-real-token") is None, \
            "3: unknown token should return None"

        # 4. an idle-expired session returns None and its row is deleted
        far_future = datetime.now() + timedelta(minutes=SESSION_IDLE_MINUTES + 1)
        assert load_session(conn, token, now=far_future) is None, \
            "4: expired session should return None"
        count = conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"]
        assert count == 0, "4: expired session row should be deleted"

        # 5. a valid read slides last_seen_at forward
        token2 = create_session(conn, "aassist", "assistant", now=datetime.now())
        before = conn.execute(
            "SELECT last_seen_at FROM sessions WHERE token_hash = ?", (_hash_token(token2),)
        ).fetchone()["last_seen_at"]
        later = datetime.fromisoformat(before) + timedelta(minutes=5)
        load_session(conn, token2, now=later)
        after = conn.execute(
            "SELECT last_seen_at FROM sessions WHERE token_hash = ?", (_hash_token(token2),)
        ).fetchone()["last_seen_at"]
        assert after == later.isoformat(), "5: load_session should slide last_seen_at forward"

        # 6. destroy_session removes the row so a following load_session returns None
        destroy_session(conn, token2)
        assert load_session(conn, token2) is None, \
            "6: destroyed session should not be loadable"

        # 7. deactivating a user rejects their live session immediately
        token3 = create_session(conn, "drossi", "dentist")
        assert load_session(conn, token3) is not None, "7: fresh session should load"
        conn.execute("UPDATE users SET active = 0 WHERE username = ?", ("drossi",))
        conn.commit()
        assert load_session(conn, token3) is None, \
            "7: session for a deactivated user must be rejected"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python web_session.py --selftest")


if __name__ == "__main__":
    main()
