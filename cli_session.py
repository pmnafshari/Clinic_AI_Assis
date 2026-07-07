import json
import sys
from datetime import datetime, timedelta
from getpass import getpass
from pathlib import Path

from werkzeug.security import check_password_hash

from auth import log_audit
from storage import init_db

DB_PATH = "db/clinic.sqlite"
SESSION_PATH = "db/session.json"
SESSION_IDLE_MINUTES = 30


def verify_credentials(username, password, conn):
    row = conn.execute(
        "SELECT password_hash, role, active FROM users WHERE username = ?", (username,)
    ).fetchone()
    if row is None or row["active"] != 1:
        return None
    if not check_password_hash(row["password_hash"], password):
        return None
    return row["role"]


def write_session(username, role, session_path=SESSION_PATH, now=None):
    if now is None:
        now = datetime.now()
    Path(session_path).parent.mkdir(parents=True, exist_ok=True)
    expires_at = now + timedelta(minutes=SESSION_IDLE_MINUTES)
    session = {"username": username, "role": role, "expires_at": expires_at.isoformat()}
    Path(session_path).write_text(json.dumps(session))


def read_session(session_path=SESSION_PATH, now=None):
    if now is None:
        now = datetime.now()

    session_file = Path(session_path)
    if not session_file.exists():
        return None
    raw = session_file.read_text().strip()
    if not raw:
        return None

    session = json.loads(raw)
    expires_at = datetime.fromisoformat(session["expires_at"])
    if now > expires_at:
        return None

    # sliding refresh - a live session's idle window resets on every read
    write_session(session["username"], session["role"], session_path, now)
    return {"username": session["username"], "role": session["role"]}


def logout(session_path=SESSION_PATH):
    session_file = Path(session_path)
    if session_file.exists():
        session_file.unlink()


def login(conn, session_path=SESSION_PATH, input_fn=input, getpass_fn=None):
    if getpass_fn is None:
        getpass_fn = getpass

    username = input_fn("username: ").strip()
    password = getpass_fn("password: ")

    role = verify_credentials(username, password, conn)
    if role is None:
        log_audit(conn, username, "unknown", "login", None, allowed=0)
        print("login failed - wrong username/password or account disabled")
        return

    write_session(username, role, session_path)
    log_audit(conn, username, role, "login", None, allowed=1)
    print(f"logged in as {username} ({role})")


def selftest():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        conn = init_db(str(Path(tmp) / "clinic.sqlite"))
        session_path = str(Path(tmp) / "session.json")

        from werkzeug.security import generate_password_hash

        conn.execute(
            "INSERT INTO users (username, password_hash, role, active) VALUES (?, ?, ?, ?)",
            ("drossi", generate_password_hash("goodpass"), "dentist", 1),
        )
        conn.execute(
            "INSERT INTO users (username, password_hash, role, active) VALUES (?, ?, ?, ?)",
            ("disabled", generate_password_hash("goodpass"), "assistant", 0),
        )
        conn.commit()

        # 1. verify_credentials: right password, wrong password, disabled account
        assert verify_credentials("drossi", "goodpass", conn) == "dentist", \
            "1: correct password should return the role"
        assert verify_credentials("drossi", "wrongpass", conn) is None, \
            "1: wrong password should return None"
        assert verify_credentials("disabled", "goodpass", conn) is None, \
            "1: active=0 user should return None"

        # 2. write_session/read_session round trip
        write_session("drossi", "dentist", session_path)
        session = read_session(session_path)
        assert session == {"username": "drossi", "role": "dentist"}, \
            "2: read_session did not round-trip username/role"

        # 3. an expired session reads back as None
        past = datetime.now() - timedelta(minutes=SESSION_IDLE_MINUTES + 1)
        write_session("drossi", "dentist", session_path, now=past)
        assert read_session(session_path) is None, "3: expired session should read as None"

        # 4. a missing session file reads back as None
        missing_path = str(Path(tmp) / "no_session.json")
        assert read_session(missing_path) is None, "4: missing session file should read as None"

        # 5. logout removes the session file
        write_session("drossi", "dentist", session_path)
        logout(session_path)
        assert not Path(session_path).exists(), "5: logout should remove the session file"

        # 6. login writes audit_log rows for both success and failure
        login(conn, session_path, input_fn=lambda p: "drossi", getpass_fn=lambda p: "goodpass")
        login(conn, session_path, input_fn=lambda p: "drossi", getpass_fn=lambda p: "wrongpass")

        rows = conn.execute(
            "SELECT username, action, allowed FROM audit_log WHERE action = 'login' ORDER BY id"
        ).fetchall()
        assert len(rows) == 2, f"6: expected 2 login audit rows, got {len(rows)}"
        assert rows[0]["allowed"] == 1, "6: successful login should log allowed=1"
        assert rows[1]["allowed"] == 0, "6: failed login should log allowed=0"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return

    if len(sys.argv) < 2:
        print("usage: python cli_session.py login|logout|whoami|--selftest")
        sys.exit(1)

    command = sys.argv[1]
    if command == "login":
        conn = init_db(DB_PATH)
        login(conn)
    elif command == "logout":
        logout()
        print("logged out")
    elif command == "whoami":
        session = read_session()
        if session is None:
            print("not logged in")
        else:
            print(f"{session['username']} ({session['role']})")
    else:
        print("usage: python cli_session.py login|logout|whoami|--selftest")
        sys.exit(1)


if __name__ == "__main__":
    main()
