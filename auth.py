import re
import sqlite3
import sys
from datetime import datetime

import clinic_time
import codice_fiscale
import patient_id

VALID_ROLES = ("dentist", "assistant", "admin")

# role -> set of allowed action strings. plain dict, no policy engine.
PERMISSIONS = {
    "dentist": {"read_notes", "append_note", "edit_note", "update_field", "update_visit_field", "add_invoice", "read_clinical", "upload_file", "issue_patient_pin", "revoke_patient_pin", "manage_appointments", "record_consent", "manage_data_requests", "file_data_request"},
    "assistant": {"read_notes", "append_note", "add_invoice", "upload_file", "issue_patient_pin", "revoke_patient_pin", "manage_appointments", "record_consent", "file_data_request"},
    # admin deliberately excluded from issue_patient_pin, revoke_patient_pin
    # and manage_appointments: it holds only manage_users and cannot open a
    # patient record at all, so granting any of them would widen admin's reach
    # into patient data. an appointment says a named person is attending this
    # clinic on a date, which is exactly that kind of data.
    "admin": {"manage_users"},
    # manage_data_requests (P06) is the dentist's alone: approving an export
    # hands over a whole record and approving an erasure destroys one, so it
    # sits with the role that already holds read_clinical.
    # system: the automated sync actor (watcher/backfill), no user row
    # reapply_erasure: a restore erasing again everyone a tombstone names
    "system": {"append_note", "reapply_erasure"},
}


# staff endpoint -> the capability its view checks. "public" needs no session,
# "login" needs any signed-in account. rbac_selftest walks every endpoint in
# the app for every role against this table, and fails on an endpoint that is
# missing from it - a new route has to be given a policy to pass the suite.
ROUTE_POLICY = {
    "static": "public",
    "shared": "public",
    "auth.login": "public",
    "auth.logout": "login",
    "auth.change_password": "login",
    "dashboard.index": "login",
    "admin.users_view": "manage_users",
    "admin.create": "manage_users",
    "admin.apply": "manage_users",
    "admin.confirm_fragment": "manage_users",
    "agent.command_page": "update_field",
    "agent.edit_page": "update_field",
    # confirm and undo act on a pending action the same user created; the
    # capability checked is the pending tool's own, re-checked in agent.py
    "agent.confirm_change": "update_field",
    "agent.undo_change": "update_field",
    "appointments.index": "manage_appointments",
    "appointments.book": "manage_appointments",
    "appointments.cancel": "manage_appointments",
    "appointments.confirm": "manage_appointments",
    "appointments.decline": "manage_appointments",
    "appointments.reschedule": "manage_appointments",
    "notes.new_note": "append_note",
    "patients.list_view": "read_notes",
    "patients.search_fragment": "read_notes",
    "patients.detail_view": "read_notes",
    "patients.edit_form_fragment": "read_notes",
    # the form is read_notes; the change it proposes is gated as update_field
    # when the pending action is built
    "patients.edit_submit": "read_notes",
    "patients.files_fragment": "read_clinical",
    "patients.visit_edit_form_fragment": "read_clinical",
    "patients.visit_edit_submit": "read_clinical",
    "patients.issue_pin_submit": "issue_patient_pin",
    "patients.revoke_pin_submit": "revoke_patient_pin",
    "patients.consent_submit": "record_consent",
    "data_requests.index": "manage_data_requests",
    "data_requests.review": "manage_data_requests",
    "data_requests.download": "manage_data_requests",
    "data_requests.file_for_patient": "file_data_request",
    # duplicate review is admin's, by the P04 decision - the one place admin
    # sees patient names and codici fiscali. recorded in P06 as an exception.
    "patients.duplicates_view": "manage_users",
    "patients.duplicates_dismiss": "manage_users",
    "patients.duplicates_merge": "manage_users",
    "qa.qa_page": "read_notes",
    "reports.index": "read_clinical",
    "upload.submit_dashboard": "upload_file",
    "upload.submit_patient": "upload_file",
    "upload.recent_intake": "upload_file",
}


def authorize(role, action):
    # unknown role -> empty set -> denies everything
    return action in PERMISSIONS.get(role, set())


def _pseudonymise(conn, text):
    # the audit trail is kept after an erasure, so an identity field must not
    # hold the one identifier that names a person. a username or target that IS
    # a codice fiscale becomes the patient's surrogate id, which stops resolving
    # to anyone once they are erased (P06). a codice fiscale inside a file path
    # is left alone here: filenames are what the intake list groups on, and the
    # erasure step redacts those rows for the patient it removes.
    if not text:
        return text
    upper = text.strip().upper()
    if not (codice_fiscale.SYNTHETIC.match(upper) or codice_fiscale.REAL.match(upper)):
        return text
    try:
        pid = patient_id.resolve(conn, upper)
    except sqlite3.OperationalError:
        # a database older than the surrogate tables; still never keep the cf
        pid = None
    return pid or "cf-unknown"


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
        ts = clinic_time.stamp()
    username = _pseudonymise(conn, username)
    target = _pseudonymise(conn, target)
    conn.execute(
        "INSERT INTO audit_log (ts, username, role, action, target, allowed, ip, reason)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ts, username, role, action, target, allowed, ip, reason),
    )
    conn.commit()


def purge_audit(conn, where, params, actor, reason):
    """Delete audit rows matching `where`, the one sanctioned way.

    Used by the retention sweep and by test harnesses removing their own
    fixtures from a dev database. The purge is itself audited, with the count,
    so a gap in the trail always has a row saying who made it and why.
    """
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("INSERT INTO audit_unlock (reason) VALUES (?)", (reason,))
        count = conn.execute(f"DELETE FROM audit_log WHERE {where}", params).rowcount
        conn.execute("DELETE FROM audit_unlock")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    log_audit(conn, actor, "system", "audit_purge", f"{count} rows", allowed=1, reason=reason)
    return count


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

        # 9. an identity field never holds a codice fiscale (P06.04): a known
        # one becomes the surrogate, an unknown one a placeholder
        pid = patient_id.seed_patient(conn, "ZZAU800101010101", "audit person")
        log_audit(conn, "ZZAU800101010101", "patient", "patient_login", "ZZAU800101010101", 1)
        log_audit(conn, "drossi", "dentist", "read_notes", "ZZAU800101010199", 0)
        log_audit(conn, "drossi", "dentist", "read_notes", f"sorted/{pid}/notes/a.json", 1)
        rows = conn.execute("SELECT username, target FROM audit_log ORDER BY id DESC LIMIT 3"
                            ).fetchall()[::-1]
        assert rows[0]["username"] == pid, f"9: patient username kept a cf: {rows[0]['username']}"
        assert rows[0]["target"] == pid, f"9: target kept a cf: {rows[0]['target']}"
        assert rows[1]["target"] == "cf-unknown", f"9: unknown cf kept: {rows[1]['target']}"
        assert rows[2]["target"] == f"sorted/{pid}/notes/a.json", "9: a path is left as written"
        assert re.match(r"^pid_[0-9a-f]{16}$", pid), "9: the surrogate is not itself cf-shaped"

        # 10. append-only: update and delete refused, purge is the one way out
        # and it leaves a row saying so
        for sql in ("UPDATE audit_log SET allowed = 1", "DELETE FROM audit_log"):
            try:
                conn.execute(sql)
                raise AssertionError(f"10: {sql} should be refused")
            except sqlite3.IntegrityError:
                pass
        conn.rollback()
        total = conn.execute("SELECT COUNT(*) c FROM audit_log").fetchone()["c"]
        gone = purge_audit(conn, "username = ?", ("u4",), "selftest", "fixture cleanup")
        assert gone == 1, f"10: purge should remove the one u4 row, removed {gone}"
        after = conn.execute("SELECT COUNT(*) c FROM audit_log").fetchone()["c"]
        assert after == total, "10: purge removes its rows and adds one saying so"
        last = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        assert last["action"] == "audit_purge" and last["target"] == "1 rows", \
            "10: the purge must audit itself"
        assert conn.execute("SELECT COUNT(*) c FROM audit_unlock").fetchone()["c"] == 0, \
            "10: the unlock must not outlive the purge"
        try:
            conn.execute("DELETE FROM audit_log")
            raise AssertionError("10: the lock must be back after a purge")
        except sqlite3.IntegrityError:
            conn.rollback()

    print("selftest passed")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python auth.py --selftest")


if __name__ == "__main__":
    main()
