"""Data-rights requests: access, export, amend, erasure (P06.05).

A request is filed by the patient from the portal or by staff on their behalf,
reviewed by someone holding manage_data_requests, and tracked to done. Nothing
happens on filing alone.

An approved access or export request builds one zip of that patient's rows and
files. It lives in exports/ for EXPORT_HOURS and is then deleted; until then
staff can download it, and so can the patient from the portal, each through
their own authenticated check. An approved erasure goes through erasure.py,
which may keep rows under a retention hold and says which and why.
"""
import io
import json
import secrets
import sys
import zipfile
from datetime import timedelta
from pathlib import Path

import clinic_time
from auth import authorize, log_audit

EXPORTS_DIR = Path(__file__).resolve().with_name("exports")
EXPORT_HOURS = 24
KINDS = ("access", "export", "amend", "erasure")

SCHEMA = """
    CREATE TABLE IF NOT EXISTS data_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('access', 'export', 'amend', 'erasure')),
        status TEXT NOT NULL DEFAULT 'open'
            CHECK (status IN ('open', 'approved', 'rejected', 'done')),
        detail TEXT,
        requested_by TEXT NOT NULL,
        requested_role TEXT NOT NULL,
        requested_at TEXT NOT NULL,
        reviewed_by TEXT,
        reviewed_at TEXT,
        reason TEXT,
        hold_reason TEXT,
        done_at TEXT,
        export_file TEXT,
        export_expires_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_data_requests_patient ON data_requests (patient_id);
"""

NOTICE = ("Copy of the data this clinic holds about you. DEMO: synthetic data from a demo "
          "clinic. Internal logs and derived search indexes are not included; ask the clinic "
          "if you want to know about them.")


def file_request(conn, pid, kind, actor, actor_role, detail=None):
    if kind not in KINDS:
        return None
    if conn.execute("SELECT 1 FROM patients WHERE patient_id = ?", (pid,)).fetchone() is None:
        return None
    # one open request of a kind at a time - a double click is not two requests
    open_row = conn.execute("SELECT id FROM data_requests WHERE patient_id = ? AND kind = ?"
                            " AND status = 'open'", (pid, kind)).fetchone()
    if open_row:
        return open_row["id"]
    cur = conn.execute(
        "INSERT INTO data_requests (patient_id, kind, detail, requested_by, requested_role,"
        " requested_at) VALUES (?, ?, ?, ?, ?, ?)",
        (pid, kind, (detail or "").strip()[:1000] or None, actor, actor_role, clinic_time.stamp()))
    conn.commit()
    log_audit(conn, actor, actor_role, "data_request", pid, allowed=1, reason=kind)
    return cur.lastrowid


def for_patient(conn, pid):
    return conn.execute("SELECT * FROM data_requests WHERE patient_id = ? ORDER BY id DESC",
                        (pid,)).fetchall()


def listing(conn):
    return conn.execute(
        "SELECT r.*, p.patient_name, p.codice_fiscale FROM data_requests r"
        " LEFT JOIN patients p ON p.patient_id = r.patient_id"
        " ORDER BY r.status != 'open', r.id DESC LIMIT 100").fetchall()


def review(conn, req_id, approve, reviewer, role, reason, sorted_root=Path("sorted"),
           exports_dir=None, collection=None):
    """Approve or reject an open request. Returns (ok, message)."""
    if not authorize(role, "manage_data_requests"):
        log_audit(conn, reviewer, role, "review_data_request", str(req_id), allowed=0)
        return False, "not permitted"
    row = conn.execute("SELECT * FROM data_requests WHERE id = ?", (req_id,)).fetchone()
    if row is None or row["status"] != "open":
        return False, "no open request with that number"
    reason = (reason or "").strip()[:500]
    if not approve and not reason:
        return False, "a refusal needs a reason the patient can be given"

    now = clinic_time.stamp()
    conn.execute("UPDATE data_requests SET status = ?, reviewed_by = ?, reviewed_at = ?,"
                 " reason = ? WHERE id = ?",
                 ("approved" if approve else "rejected", reviewer, now, reason or None, req_id))
    conn.commit()
    log_audit(conn, reviewer, role, "review_data_request", row["patient_id"], allowed=1,
              reason=f"{row['kind']}:{'approved' if approve else 'rejected'}")
    if not approve:
        return True, "request refused"

    if row["kind"] in ("access", "export"):
        name = build_export(conn, row["patient_id"], sorted_root, exports_dir)
        expires = clinic_time.to_storage(clinic_time.now_utc() + timedelta(hours=EXPORT_HOURS))
        _done(conn, req_id, export_file=name, export_expires_at=expires)
        return True, f"export ready for {EXPORT_HOURS} hours"
    if row["kind"] == "amend":
        # the correction itself goes through the normal, audited edit screens;
        # approving records that it was accepted and done
        _done(conn, req_id)
        return True, "amendment accepted - make the correction on the patient record"

    import erasure
    result = erasure.erase(conn, row["patient_id"], reviewer, role, req_id,
                           sorted_root=sorted_root, collection=collection)
    _done(conn, req_id, hold_reason=result["hold_reason"])
    if result["hold_reason"]:
        return True, f"erased, except what is held: {result['hold_reason']}"
    return True, "erased from every store"


def _done(conn, req_id, **fields):
    fields["status"] = "done"
    fields["done_at"] = clinic_time.stamp()
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE data_requests SET {sets} WHERE id = ?", (*fields.values(), req_id))
    conn.commit()


def _rows(conn, sql, pid):
    return [dict(r) for r in conn.execute(sql, (pid,)).fetchall()]


def patient_data(conn, pid):
    patient = conn.execute("SELECT patient_id, codice_fiscale, patient_name, phone FROM patients"
                           " WHERE patient_id = ?", (pid,)).fetchone()
    return {
        "notice": NOTICE,
        "exported_at": clinic_time.stamp(),
        "patient": dict(patient) if patient else None,
        "visits": _rows(conn, "SELECT visit_date, procedures, clinical_notes, next_appointment"
                              " FROM visits WHERE patient_id = ? ORDER BY visit_date", pid),
        "invoices": _rows(conn, "SELECT amount, description FROM invoices WHERE patient_id = ?"
                                " ORDER BY id", pid),
        "appointments": _rows(conn, "SELECT starts_at, minutes, status, dentist, period"
                                    " FROM appointments WHERE patient_id = ? ORDER BY id", pid),
        "consent": _rows(conn, "SELECT purpose, text_version, granted, actor_role, ts"
                               " FROM consent_records WHERE patient_id = ? ORDER BY id", pid),
        "requests": _rows(conn, "SELECT kind, status, requested_at, reviewed_at, reason,"
                                " hold_reason FROM data_requests WHERE patient_id = ?"
                                " ORDER BY id", pid),
    }


def build_export(conn, pid, sorted_root=Path("sorted"), exports_dir=None):
    exports_dir = Path(exports_dir or EXPORTS_DIR)
    exports_dir.mkdir(parents=True, exist_ok=True)
    name = f"export-{secrets.token_hex(16)}.zip"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("data.json", json.dumps(patient_data(conn, pid), indent=2, ensure_ascii=False))
        patient_dir = Path(sorted_root) / pid
        if patient_dir.is_dir():
            for f in sorted(patient_dir.rglob("*")):
                if f.is_file():
                    zf.write(f, "files/" + str(f.relative_to(patient_dir)))
    path = exports_dir / name
    path.write_bytes(buffer.getvalue())
    path.chmod(0o600)
    return name


def export_path(conn, req_id, pid=None, exports_dir=None):
    """The file of a done, unexpired export - for this patient only, if one is
    named. None for anything else, with no hint as to which."""
    row = conn.execute("SELECT * FROM data_requests WHERE id = ?", (req_id,)).fetchone()
    if row is None or row["status"] != "done" or not row["export_file"]:
        return None, None
    if pid is not None and row["patient_id"] != pid:
        return None, row
    if clinic_time.read_instant(row["export_expires_at"]) <= clinic_time.now_utc():
        return None, row
    path = Path(exports_dir or EXPORTS_DIR) / row["export_file"]
    return (path if path.is_file() else None), row


def expire_exports(conn, exports_dir=None, now=None):
    now = now or clinic_time.now_utc()
    exports_dir = Path(exports_dir or EXPORTS_DIR)
    gone = 0
    for row in conn.execute("SELECT id, export_file, export_expires_at FROM data_requests"
                            " WHERE export_file IS NOT NULL").fetchall():
        if clinic_time.read_instant(row["export_expires_at"]) > now:
            continue
        (exports_dir / row["export_file"]).unlink(missing_ok=True)
        conn.execute("UPDATE data_requests SET export_file = NULL WHERE id = ?", (row["id"],))
        gone += 1
    conn.commit()
    return gone


def selftest():
    import tempfile

    import patient_id
    from storage import init_db

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        conn = init_db(str(root / "clinic.sqlite"))
        sorted_root = root / "sorted"
        exports = root / "exports"
        pid = patient_id.seed_patient(conn, "ZZDR800101010101", "Dora Rights", "3330000001")
        other = patient_id.seed_patient(conn, "ZZDR800101010102", "Otto Other", "3330000002")
        for who, note in ((pid, "dora molar"), (other, "otto incisor")):
            cur = conn.execute("INSERT INTO visits (patient_id, visit_date, procedures,"
                               " clinical_notes, source_path) VALUES (?, '2026-05-01', '[]', ?, ?)",
                               (who, note, f"{who}.json"))
            conn.execute("INSERT INTO invoices (patient_id, visit_id, line_index, amount,"
                         " description) VALUES (?, ?, 0, 80.0, 'visita')", (who, cur.lastrowid))
            (sorted_root / who / "notes").mkdir(parents=True)
            (sorted_root / who / "notes" / f"{who}.json").write_text(note)
        conn.commit()

        # 1. filing records and audits; a repeat is the same request; nonsense is refused
        req = file_request(conn, pid, "export", pid, "patient")
        assert req and file_request(conn, pid, "export", pid, "patient") == req, "1: one open request"
        assert file_request(conn, pid, "marketing", pid, "patient") is None, "1: unknown kind"
        assert file_request(conn, "pid_0000000000000000", "export", "x", "dentist") is None, \
            "1: nobody by that id"

        # 2. only manage_data_requests reviews, and a refusal needs a reason
        assert review(conn, req, True, "aassist", "assistant", "")[1] == "not permitted", "2"
        assert not review(conn, req, False, "drossi", "dentist", "")[0], "2: bare refusal"

        # 3. the export holds this patient and nobody else
        ok, message = review(conn, req, True, "drossi", "dentist", "", sorted_root, exports)
        assert ok and "export ready" in message, f"3: {message}"
        path, row = export_path(conn, req, pid, exports)
        assert path and path.is_file() and row["status"] == "done", "3: export not ready"
        assert oct(path.stat().st_mode & 0o777) == "0o600", "3: the export is readable by owner only"
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            data = json.loads(zf.read("data.json"))
            blob = json.dumps(data) + "".join(zf.read(n).decode() for n in names if n != "data.json")
        assert data["patient"]["patient_id"] == pid and "DEMO" in data["notice"], "3: whose data"
        assert names == ["data.json", f"files/notes/{pid}.json"], f"3: {names}"
        assert "dora molar" in blob and len(data["invoices"]) == 1, "3: the patient's own data"
        assert "otto" not in blob.lower() and other not in blob, "3: another patient leaked"
        assert "pin_hash" not in blob, "3: credentials are not part of an export"

        # 4. another patient cannot reach it, and it expires and is deleted
        assert export_path(conn, req, other, exports)[0] is None, "4: another patient's export"
        later = clinic_time.now_utc() + timedelta(hours=EXPORT_HOURS + 1)
        assert expire_exports(conn, exports, now=later) == 1 and not path.exists(), "4: expiry"
        assert export_path(conn, req, pid, exports)[0] is None, "4: an expired export is gone"

        # 5. amend is accepted and tracked; review of a closed request is refused
        amend = file_request(conn, pid, "amend", pid, "patient", "my phone is 3339999999")
        assert review(conn, amend, True, "drossi", "dentist", "", sorted_root, exports)[0]
        assert not review(conn, amend, True, "drossi", "dentist", "")[0], "5: already done"
        kinds = [(r["kind"], r["status"]) for r in for_patient(conn, pid)]
        assert kinds == [("amend", "done"), ("export", "done")], f"5: {kinds}"
        audits = [r["reason"] for r in conn.execute(
            "SELECT reason FROM audit_log WHERE target = ? AND action IN"
            " ('data_request', 'review_data_request') ORDER BY id", (pid,))]
        assert audits == ["export", "export:approved", "amend", "amend:approved"], f"5: {audits}"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python data_rights.py --selftest")
    sys.exit(1)


if __name__ == "__main__":
    main()
