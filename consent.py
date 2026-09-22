"""Consent records: who agreed to what, to which wording, when, and who recorded it.

Append-only. The current state of a purpose is its newest row, so a withdrawal
is a new row, never an edit, and the history of every change stays readable.

Consent is one basis among several. Withdrawing ai_assistant stops the portal
chat; it does not delete visits, which are kept under the retention policy.

The texts in consent_texts.json are demo templates (see its _template note).
"""
import json
import sys
from pathlib import Path

import clinic_time
from auth import log_audit

TEXTS_PATH = Path(__file__).with_name("consent_texts.json")
PURPOSES = ("ai_assistant", "messaging", "recording")

SCHEMA = """
    CREATE TABLE IF NOT EXISTS consent_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT NOT NULL REFERENCES patients(patient_id),
        purpose TEXT NOT NULL,
        text_version TEXT NOT NULL,
        granted INTEGER NOT NULL CHECK (granted IN (0, 1)),
        actor TEXT NOT NULL,
        actor_role TEXT NOT NULL,
        ts TEXT NOT NULL,
        note TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_consent_patient ON consent_records (patient_id, purpose, id);
    CREATE TRIGGER IF NOT EXISTS consent_no_update BEFORE UPDATE ON consent_records
    BEGIN SELECT RAISE(ABORT, 'consent_records is append-only'); END;
    CREATE TRIGGER IF NOT EXISTS consent_no_delete BEFORE DELETE ON consent_records
    WHEN NOT EXISTS (SELECT 1 FROM audit_unlock)
    BEGIN SELECT RAISE(ABORT, 'consent_records is append-only'); END;
"""


def texts(path=None):
    return json.loads(Path(path or TEXTS_PATH).read_text())


def current(conn, pid, purpose):
    return conn.execute(
        "SELECT * FROM consent_records WHERE patient_id = ? AND purpose = ?"
        " ORDER BY id DESC LIMIT 1", (pid, purpose)).fetchone()


def allows(conn, pid, purpose):
    # a grant counts only for the wording in force now. a changed text is a
    # different thing to agree to, so the patient is asked again.
    row = current(conn, pid, purpose)
    if row is None or not row["granted"]:
        return False
    return row["text_version"] == texts()[purpose]["version"]


def state(conn, pid, lang="it"):
    wording = texts()
    out = []
    for purpose in PURPOSES:
        row = current(conn, pid, purpose)
        out.append({
            "purpose": purpose,
            "text": wording[purpose].get(lang) or wording[purpose]["it"],
            "version": wording[purpose]["version"],
            "granted": bool(row and row["granted"]),
            "recorded": row is not None,
            "outdated": bool(row and row["granted"]
                             and row["text_version"] != wording[purpose]["version"]),
            "ts": row["ts"] if row else None,
            "actor_role": row["actor_role"] if row else None,
        })
    return out


def record(conn, pid, purpose, granted, actor, actor_role, note=None):
    if purpose not in PURPOSES:
        return False, "unknown purpose"
    version = texts()[purpose]["version"]
    conn.execute(
        "INSERT INTO consent_records (patient_id, purpose, text_version, granted, actor,"
        " actor_role, ts, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (pid, purpose, version, 1 if granted else 0, actor, actor_role, clinic_time.stamp(), note))
    conn.commit()
    log_audit(conn, actor, actor_role, "consent_grant" if granted else "consent_withdraw",
              pid, allowed=1, reason=purpose)
    return True, "recorded"


def erase(conn, pids):
    # the only delete: a patient's whole consent history going with the
    # patient (erasure, or a test fixture leaving). the caller audits it.
    if not pids:
        return 0
    marks = ",".join("?" * len(pids))
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("INSERT INTO audit_unlock (reason) VALUES ('consent erase')")
        count = conn.execute(f"DELETE FROM consent_records WHERE patient_id IN ({marks})",
                             list(pids)).rowcount
        conn.execute("DELETE FROM audit_unlock")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return count


def selftest():
    import sqlite3
    import tempfile

    import patient_id
    from storage import init_db

    with tempfile.TemporaryDirectory() as tmp:
        conn = init_db(str(Path(tmp) / "clinic.sqlite"))
        pid = patient_id.seed_patient(conn, "ZZCO800101010101", "Carla Consent")

        # 1. nothing recorded is not consent
        assert not allows(conn, pid, "ai_assistant"), "1: no row must mean no consent"
        assert [s["recorded"] for s in state(conn, pid)] == [False, False, False], "1: empty state"

        # 2. a grant allows, and says who, when and to which wording
        ok, _ = record(conn, pid, "ai_assistant", True, "aassist", "assistant", "at the desk")
        assert ok and allows(conn, pid, "ai_assistant"), "2: a grant should allow"
        row = current(conn, pid, "ai_assistant")
        assert row["actor"] == "aassist" and row["text_version"] == texts()["ai_assistant"]["version"]
        assert clinic_time.has_offset(row["ts"]), "2: the time is an instant, not a wall clock"
        assert not allows(conn, pid, "messaging"), "2: one purpose does not grant another"

        # 3. a withdrawal is a new row and wins; the grant stays in the history
        record(conn, pid, "ai_assistant", False, pid, "patient")
        assert not allows(conn, pid, "ai_assistant"), "3: a withdrawal must stop it"
        rows = conn.execute("SELECT granted FROM consent_records WHERE patient_id = ?"
                            " ORDER BY id", (pid,)).fetchall()
        assert [r["granted"] for r in rows] == [1, 0], "3: history must keep both rows"

        # 4. a grant to old wording does not count once the text changes
        conn.execute("INSERT INTO consent_records (patient_id, purpose, text_version, granted,"
                     " actor, actor_role, ts) VALUES (?, 'messaging', 'demo-old', 1, 'x', 'dentist',"
                     " '2026-01-01T09:00:00+00:00')", (pid,))
        conn.commit()
        assert not allows(conn, pid, "messaging"), "4: a grant to superseded wording must not count"
        assert [s for s in state(conn, pid) if s["purpose"] == "messaging"][0]["outdated"], "4"

        # 5. append-only, and an unknown purpose is refused
        for sql in ("UPDATE consent_records SET granted = 1", "DELETE FROM consent_records"):
            try:
                conn.execute(sql)
                raise AssertionError(f"5: {sql} should be refused")
            except sqlite3.IntegrityError:
                conn.rollback()
        assert record(conn, pid, "marketing", True, "x", "dentist") == (False, "unknown purpose")

        # 6. every change is audited under the surrogate, with the purpose
        audits = conn.execute("SELECT action, target, reason FROM audit_log WHERE action LIKE"
                              " 'consent_%' ORDER BY id").fetchall()
        assert [tuple(a) for a in audits] == [("consent_grant", pid, "ai_assistant"),
                                              ("consent_withdraw", pid, "ai_assistant")], \
            f"6: {[tuple(a) for a in audits]}"

        # 7. erase takes one patient's history and nothing else, and relocks
        other = patient_id.seed_patient(conn, "ZZCO800101010102", "Other Consent")
        record(conn, other, "messaging", True, "aassist", "assistant")
        assert erase(conn, [pid]) == 3, "7: all three of the patient's rows go"
        assert allows(conn, other, "messaging"), "7: another patient's consent stays"
        try:
            conn.execute("DELETE FROM consent_records")
            raise AssertionError("7: the lock must be back after an erase")
        except sqlite3.IntegrityError:
            conn.rollback()

        # 8. the texts say what they are
        wording = texts()
        assert "DEMO TEMPLATE" in wording["_template"], "8: the texts must be labelled as a template"
        for purpose in PURPOSES:
            assert wording[purpose]["it"] and wording[purpose]["en"], f"8: {purpose} needs it and en"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python consent.py --selftest")
    sys.exit(1)


if __name__ == "__main__":
    main()
