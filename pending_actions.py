import hashlib
import json
import secrets
import sys
from datetime import datetime, timedelta

PENDING_ACTION_EXPIRY_MINUTES = 15


def _hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def create_pending_action(conn, username, role, payload, now=None):
    if now is None:
        now = datetime.now()

    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO pending_actions (token_hash, username, role, payload, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (_hash_token(token), username, role, json.dumps(payload), now.isoformat()),
    )
    conn.commit()
    return token


def load_pending_action(conn, token, username, now=None):
    if now is None:
        now = datetime.now()

    token_hash = _hash_token(token)
    # username scoping closes the IDOR gap - a token created by one user
    # must not load for another logged-in user
    row = conn.execute(
        "SELECT payload, created_at FROM pending_actions WHERE token_hash = ? AND username = ?",
        (token_hash, username),
    ).fetchone()
    if row is None:
        return None

    created_at = datetime.fromisoformat(row["created_at"])
    if now - created_at > timedelta(minutes=PENDING_ACTION_EXPIRY_MINUTES):
        # expired - fixed window, never slides on read (unlike sessions)
        conn.execute("DELETE FROM pending_actions WHERE token_hash = ?", (token_hash,))
        conn.commit()
        return None

    return json.loads(row["payload"])


def consume_pending_action(conn, token):
    conn.execute("DELETE FROM pending_actions WHERE token_hash = ?", (_hash_token(token),))
    conn.commit()


def selftest():
    import tempfile
    from pathlib import Path

    from storage import init_db

    with tempfile.TemporaryDirectory() as tmp:
        conn = init_db(str(Path(tmp) / "clinic.sqlite"))

        # 1. create_pending_action returns a raw token that is not stored literally
        payload = {"tool": "update_field", "cf": "MRRS800010150100", "args": {"field": "phone"}}
        token = create_pending_action(conn, "drossi", "dentist", payload)
        assert token, "1: create_pending_action should return a non-empty token"
        row = conn.execute("SELECT token_hash FROM pending_actions").fetchone()
        assert row["token_hash"] != token, "1: raw token must not be stored in the pending_actions table"

        # 2. load_pending_action round-trips the payload dict
        loaded = load_pending_action(conn, token, "drossi")
        assert loaded == payload, "2: load_pending_action did not round-trip the payload"

        # 3. an unknown token returns None
        assert load_pending_action(conn, "not-a-real-token", "drossi") is None, \
            "3: unknown token should return None"

        # 4. an expired pending action returns None and its row is deleted
        far_future = datetime.now() + timedelta(minutes=PENDING_ACTION_EXPIRY_MINUTES + 1)
        assert load_pending_action(conn, token, "drossi", now=far_future) is None, \
            "4: expired pending action should return None"
        count = conn.execute("SELECT COUNT(*) c FROM pending_actions").fetchone()["c"]
        assert count == 0, "4: expired pending action row should be deleted"

        # 5. a token loaded with a different username than the creator returns None
        token2 = create_pending_action(conn, "aassist", "assistant", payload)
        assert load_pending_action(conn, token2, "drossi") is None, \
            "5: token created by one user should not load for another"

        # 6. consume deletes the row so a second load returns None
        consume_pending_action(conn, token2)
        assert load_pending_action(conn, token2, "aassist") is None, \
            "6: consumed pending action should not be loadable"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python pending_actions.py --selftest")


if __name__ == "__main__":
    main()
