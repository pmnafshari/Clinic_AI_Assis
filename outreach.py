"""P29 NEW-06: the win-back list. Patients whose last visit was 12 months ago or more, with nothing booked, who agreed
to be contacted about offers and news (consent purpose "marketing", for the wording in force now). Reception or a
dentist contacts them by hand and records how it went. Nothing is sent from here - there is no messaging provider -
and a patient without that consent is never on the list, whatever else is true of them.
"""
import sys
from datetime import timedelta

import clinic_time
import consent

SCHEMA = """
    CREATE TABLE IF NOT EXISTS outreach_contacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT NOT NULL,
        outcome TEXT NOT NULL,
        note TEXT,
        by_user TEXT NOT NULL,
        at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_outreach_patient ON outreach_contacts (patient_id, at);
"""
MONTHS = 12
OUTCOMES = ("reached", "no_answer", "not_interested", "booked")


def candidates(conn, today=None, months=MONTHS):
    """[{patient_id, name, cf, phone, last_visit, last_contact}] - oldest last visit first"""
    today = today or clinic_time.now().date()
    cutoff = (today - timedelta(days=round(months * 30.44))).isoformat()
    now = clinic_time.to_storage(clinic_time.now_utc())
    rows = conn.execute(
        "SELECT p.patient_id, p.patient_name, p.codice_fiscale, p.phone, MAX(v.visit_date) AS last_visit"
        " FROM patients p JOIN visits v ON v.patient_id = p.patient_id WHERE v.visit_date IS NOT NULL"
        " GROUP BY p.patient_id HAVING last_visit <= ? ORDER BY last_visit, p.patient_name", (cutoff,)).fetchall()
    out = []
    for r in rows:
        if not consent.allows(conn, r["patient_id"], "marketing"):
            continue
        booked = conn.execute("SELECT 1 FROM appointments WHERE patient_id = ? AND status = 'booked' AND starts_at >= ?",
                              (r["patient_id"], now)).fetchone()
        if booked:
            continue
        last = conn.execute("SELECT outcome, note, by_user, at FROM outreach_contacts WHERE patient_id = ?"
                            " ORDER BY at DESC, id DESC LIMIT 1", (r["patient_id"],)).fetchone()
        out.append({"patient_id": r["patient_id"], "name": r["patient_name"], "cf": r["codice_fiscale"],
                    "phone": r["phone"], "last_visit": r["last_visit"], "last_contact": dict(last) if last else None})
    return out


def record_contact(conn, pid, outcome, who, note=None):
    """(id, None) or (None, why not). only for a patient who gave the marketing consent"""
    if outcome not in OUTCOMES:
        return None, "choose how it went"
    if not consent.allows(conn, pid, "marketing"):
        return None, "this patient has not agreed to be contacted"
    cur = conn.execute("INSERT INTO outreach_contacts (patient_id, outcome, note, by_user, at) VALUES (?, ?, ?, ?, ?)",
                       (pid, outcome, (note or "").strip()[:300] or None, who, clinic_time.stamp()))
    conn.commit()
    return cur.lastrowid, None


def on_erase(conn, pid):
    """inside the erasure transaction"""
    conn.execute("DELETE FROM outreach_contacts WHERE patient_id = ?", (pid,))


def selftest():
    import tempfile
    from datetime import date
    from pathlib import Path

    import patient_id
    from storage import init_db
    with tempfile.TemporaryDirectory() as tmp:
        conn = init_db(str(Path(tmp) / "o.sqlite"))
        today = date(2026, 11, 9)

        def patient(cf, name, last_visit, agrees, booked=False):
            pid = patient_id.seed_patient(conn, cf, name, "333 000 0000")
            if last_visit:
                conn.execute("INSERT INTO visits (patient_id, visit_date, procedures, clinical_notes, source_path)"
                             " VALUES (?, ?, '[]', '', ?)", (pid, last_visit, f"{cf}.json"))
            if agrees is not None:
                consent.record(conn, pid, "marketing", agrees, "desk", "assistant")
            if booked:
                at = clinic_time.to_storage(clinic_time.now_utc() + timedelta(days=10))
                conn.execute("INSERT INTO appointments (patient_id, dentist, starts_at, minutes, status, created_at,"
                             " updated_at) VALUES (?, 'dr', ?, 30, 'booked', ?, ?)", (pid, at, at, at))
            conn.commit()
            return pid
        old = patient("ZZOA000000000001", "Olga Antica", "2025-06-01", True)
        patient("ZZOB000000000002", "Ugo Recente", "2026-09-01", True)          # seen this year
        patient("ZZOC000000000003", "Nina Nonvuole", "2024-01-01", False)       # said no
        patient("ZZOD000000000004", "Ada Maichiesto", "2024-01-01", None)       # never asked
        patient("ZZOE000000000005", "Bea Prenotata", "2024-01-01", True, booked=True)
        patient("ZZOF000000000006", "Mai Venuta", None, True)                   # never came
        older = patient("ZZOG000000000007", "Gina Vecchia", "2023-03-01", True)
        # 1. on the list: seen 12 months ago or more, nothing booked, agreed - oldest first; nobody else
        got = [c["name"] for c in candidates(conn, today)]
        assert got == ["Gina Vecchia", "Olga Antica"], f"1: {got}"
        # 2. a withdrawn consent takes them off at once
        consent.record(conn, older, "marketing", False, "older", "patient")
        assert [c["name"] for c in candidates(conn, today)] == ["Olga Antica"], "2: HARD FAIL - a withdrawn consent"
        # 3. contacts: recorded with how it went; refused without the consent or a known outcome
        cid, why = record_contact(conn, old, "no_answer", "desk", "  ring twice  ")
        assert cid and why is None
        assert record_contact(conn, old, "maybe", "desk") == (None, "choose how it went")
        assert record_contact(conn, older, "reached", "desk") == (None, "this patient has not agreed to be contacted")
        last = candidates(conn, today)[0]["last_contact"]
        assert last["outcome"] == "no_answer" and last["note"] == "ring twice" and last["by_user"] == "desk", "3: last"
        on_erase(conn, old)
        assert conn.execute("SELECT COUNT(*) FROM outreach_contacts").fetchone()[0] == 0, "3: erasure"
        conn.close()
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        print("usage: python outreach.py --selftest")
