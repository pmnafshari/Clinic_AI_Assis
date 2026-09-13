import sys
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

from auth import VALID_ROLES
from storage import init_db

# fake dev-only credentials (CLAUDE.md: fake data through development) -
# these must be changed before any real patient data touches this database
SEED_ACCOUNTS = [
    ("dentist", "dentist", "dentist-dev-pass"),
    ("assistant", "assistant", "assistant-dev-pass"),
    ("admin", "admin", "admin-dev-pass"),
]


def seed_account(username, role, password, conn):
    if role not in VALID_ROLES:
        raise ValueError(f"role must be one of {VALID_ROLES}, got {role!r}")

    password_hash = generate_password_hash(password)
    conn.execute(
        "INSERT OR IGNORE INTO users (username, password_hash, role, active)"
        " VALUES (?, ?, ?, 1)",
        (username, password_hash, role),
    )
    conn.commit()


def seed(conn):
    for username, role, password in SEED_ACCOUNTS:
        seed_account(username, role, password, conn)


def seed_schedule(conn):
    """Dev opening hours and a roster for the seeded dentists.

    NOT the clinic's hours. D07 is unanswered, and init_db deliberately creates
    `clinic_hours` empty - an unconfigured clinic refuses every booking rather
    than behaving as though it were open around the clock. This is the dev
    fixture that makes the demo usable, and it lives here with the dev
    passwords rather than in init_db, so a real deployment never silently
    inherits somebody's guess at when it opens.

    Create-only, like seed_account: hours the clinic has already set are never
    overwritten.
    """
    import availability

    availability.seed_fixture_hours(conn)
    dentists = [r["username"] for r in conn.execute(
        "SELECT username FROM users WHERE role = 'dentist' AND active = 1")]
    for username in dentists:
        for weekday in range(5):
            closes = "17:00" if weekday == 4 else "18:00"
            conn.execute(
                "INSERT OR IGNORE INTO dentist_schedule (dentist, weekday, starts, ends)"
                " VALUES (?, ?, '09:00', ?)", (username, weekday, closes))
    conn.commit()
    return len(dentists)


def selftest():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        conn = init_db(str(Path(tmp) / "clinic.sqlite"))

        # seeding twice must stay at exactly 3 rows (re-run safe)
        seed(conn)
        seed(conn)
        count = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        assert count == 3, f"1: expected 3 users after 2 seed runs, got {count}"

        rows = conn.execute("SELECT username, password_hash, role FROM users").fetchall()
        by_username = {row["username"]: row for row in rows}

        for username, role, password in SEED_ACCOUNTS:
            row = by_username[username]
            assert row["password_hash"].startswith("scrypt:"), \
                f"2: {username} password_hash not scrypt"
            assert row["password_hash"] != password, f"2: {username} password stored as plaintext"
            assert row["role"] in VALID_ROLES, f"2: {username} role {row['role']} not valid"

        dentist_row = by_username["dentist"]
        assert check_password_hash(dentist_row["password_hash"], "dentist-dev-pass"), \
            "3: check_password_hash failed for dentist's stored hash"

        try:
            seed_account("nurse_joe", "nurse", "whatever", conn)
            raise AssertionError("4: seeding a bad role should have raised ValueError")
        except ValueError:
            pass

        # 5. SEEDING CREATES, IT DOES NOT REPAIR. INSERT OR IGNORE means an
        # existing row wins, whatever this list says - which is correct, since
        # a real deployment's roles and passwords must not be reset by a dev
        # seed list. the cost is that drift is silent: the dev db's `admin`
        # account sat on role `dentist` while this file declared `admin`, and
        # every re-seed left it that way. it was repaired by hand through
        # user_admin.set_role on 2026-09-13, after a backup snapshot.
        #
        # this check exists so the next person reads the behaviour here rather
        # than assuming a re-seed fixes a wrong role. if seeding is ever made
        # to repair, this is the assertion to change deliberately.
        conn.execute("UPDATE users SET role = 'assistant' WHERE username = 'admin'")
        conn.commit()
        seed(conn)
        drifted = conn.execute(
            "SELECT role FROM users WHERE username = 'admin'").fetchone()["role"]
        assert drifted == "assistant", \
            "5: seeding must not silently rewrite an existing account's role"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return

    Path("db").mkdir(exist_ok=True)
    conn = init_db("db/clinic.sqlite")
    seed(conn)
    seed_schedule(conn)

    count = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    print(f"seeded {count} accounts")
    print("warning: dev-only passwords - change before any real patient data is loaded")
    print("warning: dev-only opening hours and roster (Mon-Fri) - the clinic states its own")


if __name__ == "__main__":
    main()
