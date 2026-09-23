"""Retention sweep over retention.json (P06.08). The policy is a demo template.

Only types marked sweep=delete are ever deleted: expired exports, audit rows
past their period (through auth.purge_audit, which records the purge), and
closed data requests past theirs. Types marked report - invoices, clinical
records - are counted so a person can decide; nothing clinical or fiscal is
deleted by a timer.

    python retention.py            what is past its period
    python retention.py --apply    delete the sweep=delete types
"""
import json
import sys
from datetime import timedelta
from pathlib import Path

import clinic_time

ROOT = Path(__file__).resolve().parent
POLICY = ROOT / "retention.json"
DB_PATH = ROOT / "db" / "clinic.sqlite"


def policy(path=None):
    return json.loads(Path(path or POLICY).read_text())["types"]


def _cutoff(now, days):
    return clinic_time.to_storage(now - timedelta(days=days))


def plan(conn, types, now=None):
    now = now or clinic_time.now_utc()
    out = []

    def add(name, count, cutoff):
        out.append({"type": name, "sweep": types[name]["sweep"], "past_period": count,
                    "cutoff": cutoff, "keep_days": types[name]["keep_days"]})

    cut = _cutoff(now, types["audit_log"]["keep_days"])
    add("audit_log", conn.execute("SELECT COUNT(*) FROM audit_log WHERE ts < ?",
                                  (cut,)).fetchone()[0], cut)
    cut = _cutoff(now, types["closed_data_requests"]["keep_days"])
    add("closed_data_requests", conn.execute(
        "SELECT COUNT(*) FROM data_requests WHERE status IN ('done', 'rejected')"
        " AND COALESCE(done_at, reviewed_at) < ?", (cut,)).fetchone()[0], cut)
    add("exports", conn.execute(
        "SELECT COUNT(*) FROM data_requests WHERE export_file IS NOT NULL"
        " AND export_expires_at < ?", (clinic_time.to_storage(now),)).fetchone()[0],
        clinic_time.to_storage(now))
    day = (now - timedelta(days=types["clinical_records"]["keep_days"])).date().isoformat()
    add("clinical_records", conn.execute(
        "SELECT COUNT(*) FROM visits WHERE visit_date < ?", (day,)).fetchone()[0], day)
    # P13: next-visit summaries are clinical records and follow that policy -
    # counted for a person, never deleted by the sweep
    cut = _cutoff(now, types["clinical_records"]["keep_days"])
    out.append({"type": "clinical_summaries", "sweep": types["clinical_records"]["sweep"],
                "past_period": conn.execute(
                    "SELECT COUNT(*) FROM visit_summaries WHERE created_at < ?",
                    (cut,)).fetchone()[0],
                "cutoff": cut, "keep_days": types["clinical_records"]["keep_days"]})
    # POL-9: uploads awaiting review, rejected, or unreadable are clinical
    # material too - counted, never swept
    out.append({"type": "staged_notes", "sweep": types["clinical_records"]["sweep"],
                "past_period": conn.execute(
                    "SELECT COUNT(*) FROM note_reviews WHERE origin = 'upload' AND status IN"
                    " ('pending', 'rejected', 'extraction_failed') AND created_at < ?",
                    (cut,)).fetchone()[0],
                "cutoff": cut, "keep_days": types["clinical_records"]["keep_days"]})
    # P15: patient documents are clinical records - counted, never swept
    out.append({"type": "patient_documents", "sweep": types["clinical_records"]["sweep"],
                "past_period": conn.execute(
                    "SELECT COUNT(*) FROM patient_documents WHERE uploaded_at < ?",
                    (cut,)).fetchone()[0],
                "cutoff": cut, "keep_days": types["clinical_records"]["keep_days"]})
    if "staff_sessions" in types:
        # the app's own idle rule, counted with the same scan the sweep uses
        import web_session
        out.append({"type": "staff_sessions", "sweep": types["staff_sessions"]["sweep"],
                    "past_period": len(web_session.select_expired(conn, now)),
                    "cutoff": clinic_time.to_storage(
                        now - timedelta(minutes=web_session.SESSION_IDLE_MINUTES)),
                    "keep_days": types["staff_sessions"]["keep_days"]})
    day = (now - timedelta(days=types["invoices"]["keep_days"])).date().isoformat()
    add("invoices", conn.execute(
        "SELECT COUNT(*) FROM invoices i JOIN visits v ON v.id = i.visit_id"
        " WHERE v.visit_date < ?", (day,)).fetchone()[0], day)
    return out


def apply(conn, types, now=None, exports_dir=None):
    import data_rights
    from auth import purge_audit
    now = now or clinic_time.now_utc()
    done = {}
    for row in plan(conn, types, now):
        if row["sweep"] != "delete" or not row["past_period"]:
            continue
        if row["type"] == "audit_log":
            done["audit_log"] = purge_audit(conn, "ts < ?", (row["cutoff"],), "retention",
                                            "retention period")
        elif row["type"] == "closed_data_requests":
            done["closed_data_requests"] = conn.execute(
                "DELETE FROM data_requests WHERE status IN ('done', 'rejected')"
                " AND COALESCE(done_at, reviewed_at) < ?", (row["cutoff"],)).rowcount
            conn.commit()
        elif row["type"] == "exports":
            done["exports"] = data_rights.expire_exports(conn, exports_dir, now=now)
        elif row["type"] == "staff_sessions":
            import web_session
            done["staff_sessions"] = web_session.expire_idle(conn, now=now)["deleted"]
    return done


def selftest():
    import tempfile

    import patient_id
    from storage import init_db

    with tempfile.TemporaryDirectory() as tmp:
        conn = init_db(str(Path(tmp) / "clinic.sqlite"))
        types = policy()
        now = clinic_time.now_utc()
        longest = max(types["audit_log"]["keep_days"], types["closed_data_requests"]["keep_days"])
        old = clinic_time.to_storage(now - timedelta(days=longest + 5))
        pid = patient_id.seed_patient(conn, "ZZRT800101010101", "Rosa Retain")
        conn.execute("INSERT INTO audit_unlock (reason) VALUES ('seed')")
        conn.execute("INSERT INTO audit_log (ts, username, role, action, allowed)"
                     " VALUES (?, 'drossi', 'dentist', 'login', 1)", (old,))
        conn.execute("DELETE FROM audit_unlock")
        cur = conn.execute("INSERT INTO visits (patient_id, visit_date, procedures,"
                           " clinical_notes, source_path) VALUES (?, '2001-01-01', '[]', 'x',"
                           " 'r.json')", (pid,))
        conn.execute("INSERT INTO invoices (patient_id, visit_id, line_index, amount)"
                     " VALUES (?, ?, 0, 50)", (pid, cur.lastrowid))
        conn.execute("INSERT INTO data_requests (patient_id, kind, status, requested_by,"
                     " requested_role, requested_at, done_at) VALUES (?, 'access', 'done', ?,"
                     " 'patient', ?, ?)", (pid, pid, old, old))
        # a staff session idle for days: dead by the app's own rule
        conn.execute("INSERT INTO sessions (token_hash, username, role, created_at, last_seen_at)"
                     " VALUES ('rt-h', 'drossi', 'dentist', ?, ?)", (old, old))
        # P13: an old next-visit summary, counted with clinical records
        conn.execute("INSERT INTO visit_summaries (patient_id, status, generator,"
                     " generator_version, source_ids, source_fingerprint, created_by,"
                     " created_at) VALUES (?, 'approved', 'extractive', 't', '[]', 't',"
                     " 'drossi', '2001-01-02T08:00:00+00:00')", (pid,))
        conn.commit()
        from auth import log_audit
        log_audit(conn, "drossi", "dentist", "login", None, 1)

        # 1. the plan counts each type against its own period
        counts = {r["type"]: r["past_period"] for r in plan(conn, types, now)}
        assert counts == {"audit_log": 1, "closed_data_requests": 1, "exports": 0,
                          "clinical_records": 1, "clinical_summaries": 1, "staged_notes": 0,
                          "patient_documents": 0, "staff_sessions": 1, "invoices": 1}, \
            f"1: {counts}"

        # 2. apply deletes only sweep=delete types, and the audit purge says so
        done = apply(conn, types, now, exports_dir=Path(tmp) / "exports")
        assert done == {"audit_log": 1, "closed_data_requests": 1, "staff_sessions": 1}, f"2: {done}"
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0, "2: session kept"
        assert conn.execute("SELECT COUNT(*) FROM visits").fetchone()[0] == 1, \
            "2: clinical records are never swept"
        assert conn.execute("SELECT COUNT(*) FROM visit_summaries").fetchone()[0] == 1, \
            "2: summaries are clinical records and are never swept"
        assert conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 1, \
            "2: invoices are never swept"
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'audit_purge'"
                            " AND reason = 'retention period'").fetchone()[0] == 1, "2"
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'login'"
                            ).fetchone()[0] == 1, "2: the recent row stays"

        # 3. nothing clinical or fiscal is marked for deletion in the policy
        for name in ("invoices", "clinical_records"):
            assert types[name]["sweep"] == "report", f"3: {name} must stay report-only"
        assert "DEMO TEMPLATE" in json.loads(POLICY.read_text())["_template"], "3"

    print("selftest ok")


def main(argv):
    if "--selftest" in argv:
        selftest()
        return
    import storage
    conn = storage.init_db(str(DB_PATH))
    try:
        types = policy()
        report = {"plan": plan(conn, types)}
        if "--apply" in argv:
            report["deleted"] = apply(conn, types)
        else:
            # the dry run of the session sweep is audited as a dry run
            import web_session
            report["staff_sessions_dry_run"] = web_session.expire_idle(conn, dry_run=True)
        print(json.dumps(report, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main(sys.argv[1:])
