"""Give every visit that already has invoice lines a ledger invoice (P07.03).

They are created as `unknown`: whether they were paid is not recorded anywhere,
so the migration assumes neither paid nor unpaid. None of them counts as owed
until a person reconciles it on the billing screen. An encrypted backup is
taken first; running it again changes nothing.

    python migrate_ledger.py            report only
    python migrate_ledger.py --apply    back up, then create
"""
import json
import sys
from pathlib import Path

import backup
import ledger
import storage
from auth import log_audit

DB_PATH = "db/clinic.sqlite"


def plan(conn):
    return conn.execute(
        "SELECT DISTINCT i.patient_id, i.visit_id FROM invoices i"
        " WHERE i.visit_id NOT IN (SELECT visit_id FROM billing_invoices)"
        " ORDER BY i.visit_id").fetchall()


def run(db_path=DB_PATH, do_apply=False, data_root=".", key_path=None):
    conn = storage.init_db(db_path)
    try:
        todo = plan(conn)
        report = {"legacy_invoices": len(todo), "applied": False}
        if not do_apply or not todo:
            report["applied"] = do_apply
            return report
        report["backup"] = backup.create(data_root=data_root, key_path=key_path)["archive"]
        conn.execute("BEGIN IMMEDIATE")
        try:
            for row in todo:
                ledger.ensure_invoice(conn, row["patient_id"], row["visit_id"], legacy=True)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        log_audit(conn, "migrate_ledger", "system", "ledger_migrate", f"{len(todo)} invoices",
                  allowed=1, reason="legacy invoices created as unknown")
        report["applied"] = True
        report["left"] = len(plan(conn))
        return report
    finally:
        conn.close()


def selftest():
    import tempfile

    import patient_id

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "db").mkdir()
        key = root / "k.key"
        backup.init_key(key)
        db = root / "db" / "clinic.sqlite"
        conn = storage.init_db(str(db))
        pid = patient_id.seed_patient(conn, "ZZML800101010101", "Mia Migrate")
        for day, amount in (("2025-03-01", 120.5), ("2025-04-01", 0.1)):
            vid = conn.execute("INSERT INTO visits (patient_id, visit_date, procedures,"
                               " clinical_notes, source_path) VALUES (?, ?, '[]', '', ?)",
                               (pid, day, day)).lastrowid
            # rows as the old code wrote them: a float, no cents, no ledger row
            conn.execute("INSERT INTO invoices (patient_id, visit_id, line_index, amount)"
                         " VALUES (?, ?, 0, ?)", (pid, vid, amount))
        conn.commit()
        conn.close()

        report = run(str(db), data_root=root, key_path=key)
        assert report == {"legacy_invoices": 2, "applied": False}, f"1: {report}"
        report = run(str(db), do_apply=True, data_root=root, key_path=key)
        assert report["applied"] and report["left"] == 0 and Path(report["backup"]).exists(), report

        conn = storage.connect(str(db))
        summary = ledger.patient_summary(conn, pid)
        assert [i["state"] for i in summary["invoices"]] == ["unknown", "unknown"], summary
        assert summary["outstanding_cents"] == 0 and summary["unknown"] == 2, \
            "2: legacy money is never turned into a debt"
        assert [i["total_cents"] for i in summary["invoices"]] == [12050, 10], \
            "2: the float amounts became exact cents"
        conn.close()
        assert run(str(db), do_apply=True, data_root=root, key_path=key)["legacy_invoices"] == 0

    print("selftest ok")


def main(argv):
    if "--selftest" in argv:
        selftest()
        return
    print(json.dumps(run(do_apply="--apply" in argv), indent=2))


if __name__ == "__main__":
    main(sys.argv[1:])
