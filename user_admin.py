import sqlite3
import sys

from werkzeug.security import generate_password_hash

import web_session
from auth import VALID_ROLES, authorize, log_audit


def _guard(conn, actor, actor_role, action, target, self_check=True):
    # returns an error message, or None when the caller may proceed.
    # every refusal is audited - a denied attempt is still an attempt.
    if not authorize(actor_role, "manage_users"):
        log_audit(conn, actor, actor_role, action, target, allowed=0)
        return f"not permitted: {actor_role} may not manage_users"
    if self_check and target == actor:
        log_audit(conn, actor, actor_role, action, target, allowed=0)
        return "not permitted: cannot change your own account"
    return None


def list_users(conn):
    return conn.execute(
        "SELECT username, role, active, failed_attempts, locked_until"
        " FROM users ORDER BY username"
    ).fetchall()


def get_user(conn, username):
    return conn.execute(
        "SELECT username, role, active, failed_attempts, locked_until"
        " FROM users WHERE username = ?",
        (username,),
    ).fetchone()


def create_user(conn, username, role, password, actor, actor_role):
    username = (username or "").strip()
    # self_check off: creating an account named after yourself just collides on
    # the UNIQUE index, and blocking it here would report the wrong reason
    error = _guard(conn, actor, actor_role, "create_user", username, self_check=False)
    if error:
        return False, error

    if not username:
        return False, "Enter a username."
    if role not in VALID_ROLES:
        return False, f"role must be one of {VALID_ROLES}"
    if not password:
        return False, "Enter a password for the new account."

    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, role, active, must_change_password)"
            " VALUES (?, ?, ?, 1, 1)",
            (username, generate_password_hash(password), role),
        )
    except sqlite3.IntegrityError:
        return False, "That username is already taken."

    conn.commit()
    log_audit(conn, actor, actor_role, "create_user", username, allowed=1)
    return True, f"{username} created."


def set_role(conn, username, new_role, actor, actor_role):
    error = _guard(conn, actor, actor_role, "set_role", username)
    if error:
        return False, error
    if new_role not in VALID_ROLES:
        return False, f"role must be one of {VALID_ROLES}"

    cur = conn.execute("UPDATE users SET role = ? WHERE username = ?", (new_role, username))
    if cur.rowcount == 0:
        return False, "no such user"

    conn.commit()
    log_audit(conn, actor, actor_role, "set_role", f"{username}:{new_role}", allowed=1)
    return True, f"Role changed to {new_role}."


def set_active(conn, username, active, actor, actor_role):
    error = _guard(conn, actor, actor_role, "set_active", username)
    if error:
        return False, error

    active = 1 if active else 0
    cur = conn.execute("UPDATE users SET active = ? WHERE username = ?", (active, username))
    if cur.rowcount == 0:
        return False, "no such user"

    # disabling and revoking must not be separable - a disabled account with a
    # live cookie is the whole thing we are preventing
    if not active:
        web_session.destroy_user_sessions(conn, username)

    conn.commit()
    state = "active" if active else "disabled"
    log_audit(conn, actor, actor_role, "set_active", f"{username}:{state}", allowed=1)
    return True, f"Account {state}."


def clear_lockout(conn, username, actor, actor_role):
    error = _guard(conn, actor, actor_role, "clear_lockout", username)
    if error:
        return False, error

    cur = conn.execute(
        "UPDATE users SET failed_attempts = 0, locked_until = NULL WHERE username = ?",
        (username,),
    )
    if cur.rowcount == 0:
        return False, "no such user"

    conn.commit()
    log_audit(conn, actor, actor_role, "clear_lockout", username, allowed=1)
    return True, "Lockout cleared."


def _audit_count(conn, allowed=None):
    if allowed is None:
        return conn.execute("SELECT COUNT(*) c FROM audit_log").fetchone()["c"]
    return conn.execute(
        "SELECT COUNT(*) c FROM audit_log WHERE allowed = ?", (allowed,)
    ).fetchone()["c"]


def selftest():
    import tempfile
    from pathlib import Path

    from storage import init_db

    with tempfile.TemporaryDirectory() as tmp:
        conn = init_db(str(Path(tmp) / "clinic.sqlite"))
        conn.execute(
            "INSERT INTO users (username, password_hash, role, active) VALUES (?, ?, ?, 1)",
            ("admin", "x", "admin"),
        )
        conn.execute(
            "INSERT INTO users (username, password_hash, role, active) VALUES (?, ?, ?, 1)",
            ("drossi", "x", "dentist"),
        )
        conn.commit()

        # 1. admin creates an account: scrypt hash, forced first-login change
        ok, msg = create_user(conn, "newstaff", "assistant", "temp-pass", "admin", "admin")
        assert ok, f"1: create_user failed: {msg}"
        row = get_user(conn, "newstaff")
        assert row["role"] == "assistant", "1: role not stored"
        stored = conn.execute(
            "SELECT password_hash, must_change_password FROM users WHERE username = ?",
            ("newstaff",),
        ).fetchone()
        assert stored["password_hash"].startswith("scrypt:"), "1: password not scrypt-hashed"
        assert stored["must_change_password"] == 1, "1: must_change_password not set on create"

        # 2. a non-admin is denied every mutation, and each denial is audited
        for call in (
            lambda: create_user(conn, "x1", "assistant", "p", "drossi", "dentist"),
            lambda: set_role(conn, "newstaff", "dentist", "drossi", "dentist"),
            lambda: set_active(conn, "newstaff", 0, "drossi", "dentist"),
            lambda: clear_lockout(conn, "newstaff", "drossi", "dentist"),
        ):
            before = _audit_count(conn, allowed=0)
            ok, msg = call()
            assert not ok, f"2: dentist should be denied, got ok ({msg})"
            assert "not permitted" in msg, f"2: unclear denial message: {msg}"
            assert _audit_count(conn, allowed=0) == before + 1, \
                "2: denial must write an allowed=0 audit row"

        # 3. an admin cannot disable themselves
        before = _audit_count(conn, allowed=0)
        ok, msg = set_active(conn, "admin", 0, "admin", "admin")
        assert not ok and "your own account" in msg, f"3: self-disable not blocked: {msg}"
        assert get_user(conn, "admin")["active"] == 1, "3: admin was disabled anyway"
        assert _audit_count(conn, allowed=0) == before + 1, "3: self-disable must be audited"

        # 4. an admin cannot demote themselves - this is what keeps one admin alive
        ok, msg = set_role(conn, "admin", "assistant", "admin", "admin")
        assert not ok and "your own account" in msg, f"4: self-demote not blocked: {msg}"
        assert get_user(conn, "admin")["role"] == "admin", "4: admin demoted themselves"
        admins = conn.execute(
            "SELECT COUNT(*) c FROM users WHERE role = 'admin' AND active = 1"
        ).fetchone()["c"]
        assert admins >= 1, "4: no active admin left"

        # 5. disabling a user drops their sessions but not anyone else's
        web_session.create_session(conn, "newstaff", "assistant")
        web_session.create_session(conn, "newstaff", "assistant")
        keep = web_session.create_session(conn, "drossi", "dentist")
        ok, msg = set_active(conn, "newstaff", 0, "admin", "admin")
        assert ok, f"5: disable failed: {msg}"
        gone = conn.execute(
            "SELECT COUNT(*) c FROM sessions WHERE username = ?", ("newstaff",)
        ).fetchone()["c"]
        assert gone == 0, "5: disabled user still has sessions"
        assert web_session.load_session(conn, keep) is not None, \
            "5: another user's session was destroyed"

        # 6. re-enabling works and duplicate usernames are refused
        ok, _ = set_active(conn, "newstaff", 1, "admin", "admin")
        assert ok and get_user(conn, "newstaff")["active"] == 1, "6: re-enable failed"
        ok, msg = create_user(conn, "newstaff", "dentist", "p", "admin", "admin")
        assert not ok and "already taken" in msg, f"6: duplicate username not refused: {msg}"

        # 7. an unknown role is refused before it reaches the DB CHECK
        ok, msg = set_role(conn, "newstaff", "wizard", "admin", "admin")
        assert not ok and "role must be one of" in msg, f"7: bad role accepted: {msg}"
        assert get_user(conn, "newstaff")["role"] == "assistant", "7: role changed anyway"

        # 8. clear_lockout zeroes the counter and the timestamp
        conn.execute(
            "UPDATE users SET failed_attempts = 5, locked_until = ? WHERE username = ?",
            ("2030-01-01T00:00:00", "newstaff"),
        )
        conn.commit()
        ok, msg = clear_lockout(conn, "newstaff", "admin", "admin")
        assert ok, f"8: clear_lockout failed: {msg}"
        row = get_user(conn, "newstaff")
        assert row["failed_attempts"] == 0 and row["locked_until"] is None, \
            "8: lockout not cleared"

        # 9. a missing user is reported, not silently ignored
        ok, msg = set_role(conn, "ghost", "dentist", "admin", "admin")
        assert not ok and msg == "no such user", f"9: missing user not reported: {msg}"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python user_admin.py --selftest")


if __name__ == "__main__":
    main()
