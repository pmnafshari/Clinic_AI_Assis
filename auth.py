import sys
from datetime import datetime

VALID_ROLES = ("dentist", "assistant", "admin")

# role -> set of allowed action strings. plain dict, no policy engine.
PERMISSIONS = {
    "dentist": {"read_notes", "append_note", "edit_note", "update_field", "update_visit_field", "add_invoice", "read_clinical", "upload_file", "issue_patient_pin", "revoke_patient_pin", "manage_appointments"},
    "assistant": {"read_notes", "append_note", "add_invoice", "upload_file", "issue_patient_pin", "revoke_patient_pin", "manage_appointments"},
    # admin deliberately excluded from issue_patient_pin, revoke_patient_pin
    # and manage_appointments: it holds only manage_users and cannot open a
    # patient record at all, so granting any of them would widen admin's reach
    # into patient data. an appointment says a named person is attending this
    # clinic on a date, which is exactly that kind of data.
    "admin": {"manage_users"},
    # system: the automated sync actor (watcher/backfill), no user row
    "system": {"append_note"},
}


def authorize(role, action):
    # unknown role -> empty set -> denies everything
    return action in PERMISSIONS.get(role, set())


def log_audit(conn, username, role, action, target, allowed, ts=None, ip=None, reason=None):
    # ip: the source address behind the row. the patient surface sets it on
    # login, logout, chat and scope-violation rows; staff paths still pass None.
    # it is resolved through patient_app/net.py, so it is the patient's own
    # address only when that app is configured to trust the tunnel's forwarded
    # header - otherwise it is the socket peer, which behind a tunnel is the
    # tunnel itself. evidence either way, never identity.
    # a sweep with no source recorded is invisible after the fact
    # reason: a closed-vocabulary category explaining why a file was routed
    # where it was, surfaced to staff on the intake list. NEVER raw exception
    # text - extract_note's ValueError can carry a codice fiscale. sort_files
    # owns the vocabulary.
    if ts is None:
        ts = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO audit_log (ts, username, role, action, target, allowed, ip, reason)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ts, username, role, action, target, allowed, ip, reason),
    )
    conn.commit()


def selftest():
    from storage import init_db
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        conn = init_db(str(Path(tmp) / "clinic.sqlite"))

        assert authorize("dentist", "update_field"), "1: dentist should allow update_field"
        assert authorize("dentist", "update_visit_field"), "1: dentist should allow update_visit_field"
        assert authorize("dentist", "add_invoice"), "1: dentist should allow add_invoice"
        assert authorize("dentist", "append_note"), "1: dentist should allow append_note"
        assert authorize("dentist", "edit_note"), "1: dentist should allow edit_note"
        assert authorize("dentist", "read_clinical"), "1: dentist should allow read_clinical"
        assert authorize("dentist", "upload_file"), "1: dentist should allow upload_file"
        assert authorize("dentist", "issue_patient_pin"), "1: dentist should allow issue_patient_pin"
        assert not authorize("dentist", "manage_users"), "1: dentist should deny manage_users"

        assert authorize("assistant", "append_note"), "2: assistant should allow append_note"
        assert authorize("assistant", "add_invoice"), "2: assistant should allow add_invoice"
        assert authorize("assistant", "upload_file"), "2: assistant should allow upload_file"
        assert authorize("assistant", "issue_patient_pin"), "2: assistant should allow issue_patient_pin"
        assert not authorize("assistant", "edit_note"), "2: assistant should deny edit_note"
        assert not authorize("assistant", "update_field"), "2: assistant should deny update_field"
        assert not authorize("assistant", "update_visit_field"), "2: assistant should deny update_visit_field"
        assert not authorize("assistant", "read_clinical"), "2: assistant should deny read_clinical"
        assert not authorize("assistant", "manage_users"), "2: assistant should deny manage_users"

        assert authorize("admin", "manage_users"), "3: admin should allow manage_users"
        assert not authorize("admin", "issue_patient_pin"), "3: admin should deny issue_patient_pin"
        assert not authorize("admin", "update_field"), "3: admin should deny update_field"
        assert not authorize("admin", "append_note"), "3: admin should deny append_note"
        assert not authorize("admin", "add_invoice"), "3: admin should deny add_invoice"
        assert not authorize("admin", "read_notes"), "3: admin should deny read_notes"
        assert not authorize("admin", "read_clinical"), "3: admin should deny read_clinical"
        assert not authorize("admin", "upload_file"), "3: admin should deny upload_file"

        assert not authorize("nobody", "read_notes"), "4: unknown role should deny everything"

        assert authorize("system", "append_note"), "6: system should allow append_note"
        assert not authorize("system", "read_notes"), "6: system should deny read_notes"
        assert not authorize("system", "edit_note"), "6: system should deny edit_note"
        assert not authorize("system", "update_field"), "6: system should deny update_field"
        assert not authorize("system", "add_invoice"), "6: system should deny add_invoice"
        assert not authorize("system", "read_clinical"), "6: system should deny read_clinical"
        assert not authorize("system", "upload_file"), "6: system should deny upload_file"
        assert not authorize("system", "manage_users"), "6: system should deny manage_users"
        assert "system" not in VALID_ROLES, "6: system must not become a creatable user role"

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

        # 7. log_audit takes an optional source address (D-06). every existing
        # caller passes no ip and must keep storing NULL - the patient login
        # surface is the only caller that will ever set it.
        log_audit(conn, "u", "patient", "patient_pin_check", "CF", allowed=0, ip="203.0.113.9")
        ip_row = conn.execute(
            "SELECT * FROM audit_log WHERE action = 'patient_pin_check' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert ip_row["ip"] == "203.0.113.9", "7: ip should round-trip"

        log_audit(conn, "drossi", "dentist", "update_field", "MRRS800010150100", 1)
        no_ip_row = conn.execute(
            "SELECT * FROM audit_log WHERE username = 'drossi' AND action = 'update_field'"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert no_ip_row["ip"] is None, "7: a call with no ip= must store NULL"

        audit_columns = {row["name"] for row in conn.execute("PRAGMA table_info(audit_log)")}
        assert "ip" in audit_columns, "7: audit_log should have an ip column"

        # 8. log_audit takes an optional reason (GUI-10). it is the last
        # parameter on purpose - anywhere earlier would shift ts/ip for the
        # positional callers above.
        log_audit(conn, "u2", "assistant", "upload_file", "sorted/needs_review/x.txt",
                  allowed=1, reason="the model could not read this note")
        reason_row = conn.execute(
            "SELECT * FROM audit_log WHERE username = 'u2' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert reason_row["reason"] == "the model could not read this note", \
            "8: reason should round-trip exactly"

        log_audit(conn, "u3", "assistant", "upload_file", "sorted/CF/notes/y.txt", allowed=1)
        no_reason_row = conn.execute(
            "SELECT * FROM audit_log WHERE username = 'u3' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert no_reason_row["reason"] is None, "8: a call with no reason= must store NULL"

        # the new trailing parameter must not have shifted ip for a caller
        # using the pre-existing positional+ip= style
        log_audit(conn, "u4", "patient", "patient_pin_check", "CF", allowed=0, ip="198.51.100.7")
        shift_row = conn.execute(
            "SELECT * FROM audit_log WHERE username = 'u4' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert shift_row["ip"] == "198.51.100.7", "8: adding reason must not shift ip"
        assert shift_row["reason"] is None, "8: that caller supplied no reason"

        assert "reason" in audit_columns, "8: audit_log should have a reason column"

    print("selftest passed")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python auth.py --selftest")


if __name__ == "__main__":
    main()
