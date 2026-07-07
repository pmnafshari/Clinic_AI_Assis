import sys
from datetime import datetime

VALID_ROLES = ("dentist", "assistant", "admin")

# role -> set of allowed action strings. plain dict, no policy engine.
PERMISSIONS = {
    "dentist": {"read_notes", "append_note", "edit_note", "update_field", "add_invoice"},
    "assistant": {"read_notes", "append_note", "add_invoice"},
    "admin": {"manage_users"},
}


def authorize(role, action):
    # unknown role -> empty set -> denies everything
    return action in PERMISSIONS.get(role, set())


def log_audit(conn, username, role, action, target, allowed, ts=None):
    if ts is None:
        ts = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO audit_log (ts, username, role, action, target, allowed)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (ts, username, role, action, target, allowed),
    )
    conn.commit()


def selftest():
    from storage import init_db
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        conn = init_db(str(Path(tmp) / "clinic.sqlite"))

        assert authorize("dentist", "update_field"), "1: dentist should allow update_field"
        assert authorize("dentist", "add_invoice"), "1: dentist should allow add_invoice"
        assert authorize("dentist", "append_note"), "1: dentist should allow append_note"
        assert authorize("dentist", "edit_note"), "1: dentist should allow edit_note"
        assert not authorize("dentist", "manage_users"), "1: dentist should deny manage_users"

        assert authorize("assistant", "append_note"), "2: assistant should allow append_note"
        assert authorize("assistant", "add_invoice"), "2: assistant should allow add_invoice"
        assert not authorize("assistant", "edit_note"), "2: assistant should deny edit_note"
        assert not authorize("assistant", "update_field"), "2: assistant should deny update_field"
        assert not authorize("assistant", "manage_users"), "2: assistant should deny manage_users"

        assert authorize("admin", "manage_users"), "3: admin should allow manage_users"
        assert not authorize("admin", "update_field"), "3: admin should deny update_field"
        assert not authorize("admin", "append_note"), "3: admin should deny append_note"
        assert not authorize("admin", "add_invoice"), "3: admin should deny add_invoice"
        assert not authorize("admin", "read_notes"), "3: admin should deny read_notes"

        assert not authorize("nobody", "read_notes"), "4: unknown role should deny everything"

        log_audit(conn, "drossi", "dentist", "update_field", "MRRS800010150100", 1)
        log_audit(conn, "aassist", "assistant", "edit_note", "MRRS800010150100", 0)

        rows = conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        assert len(rows) == 2, f"5: expected 2 audit_log rows, got {len(rows)}"
        assert rows[0]["allowed"] == 1, "5: first row should be allowed=1"
        assert rows[1]["allowed"] == 0, "5: second row should be allowed=0"
        for row in rows:
            assert row["ts"], "5: ts must not be null/empty"
            assert row["username"], "5: username must not be null/empty"
            assert row["action"], "5: action must not be null/empty"

    print("selftest passed")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python auth.py --selftest")


if __name__ == "__main__":
    main()
