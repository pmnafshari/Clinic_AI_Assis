"""independent restore drill (P17.04 / P17.T3). counts and timings only.

    .venv/bin/python restore_drill.py [--no-new-backup] [--rollback-ref <commit>] [--out FILE]
    .venv/bin/python restore_drill.py --selftest

1. RPO: how old the newest backup was when the drill started - the data a
   restore at that moment would have lost. no backup schedule is installed, so
   there is no bound on it except running backup.py create.
2. a fresh backup (unless --no-new-backup), restored into a new temporary
   folder through backup.restore: verified, erasures re-applied, index rebuilt.
   RTO is the wall time of that restore.
3. deletion after restore: one patient from the backup is named in a drill-only
   tombstone (a temporary file - the live tombstones are never written) and
   must be gone from the restored copy, with nobody else touched.
4. the rebuilt index answers a search and holds nothing of that patient.
5. rollback rehearsal (--rollback-ref): the given earlier commit, checked out in
   a temporary worktree, opens a copy of the restored database and serves the
   staff login page.
the live data is only read. everything restored is deleted at the end.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import backup
import clinic_time

ROOT = Path(__file__).resolve().parent


def newest_archive(dest):
    archives = sorted(Path(dest).glob(f"{backup.NAME_PREFIX}*{backup.SUFFIX}"), key=lambda p: p.stat().st_mtime)
    return archives[-1] if archives else None


def pick_patient(db_path):
    # read-only: the patient with the most visits, so the erasure check has something to remove
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT p.patient_id, p.codice_fiscale, COUNT(v.id) FROM patients p"
                           " LEFT JOIN visits v ON v.patient_id = p.patient_id GROUP BY p.patient_id"
                           " ORDER BY COUNT(v.id) DESC, p.patient_id LIMIT 1").fetchone()
    finally:
        conn.close()
    return row


def rehearse_rollback(ref, restored_db, work):
    tree = Path(work) / "rollback-tree"
    subprocess.run(["git", "-C", str(ROOT), "worktree", "add", "--detach", str(tree), ref],
                   check=True, capture_output=True)
    try:
        db_copy = Path(work) / "rollback.sqlite"
        shutil.copy2(restored_db, db_copy)
        probe = ("import sys, sqlite3\n"
                 "import storage, app.db as d\n"
                 f"d.DB_PATH = {str(db_copy)!r}\n"
                 f"storage.init_db({str(db_copy)!r}).close()\n"
                 "from app import create_app\n"
                 "r = create_app().test_client().get('/login')\n"
                 "c = sqlite3.connect(d.DB_PATH)\n"
                 "print(r.status_code, c.execute('PRAGMA integrity_check').fetchone()[0],"
                 " c.execute('SELECT COUNT(*) FROM visits').fetchone()[0])\n")
        env = {**os.environ, "DISK_GUARD_DISARMED": "1"}
        done = subprocess.run([sys.executable, "-c", probe], cwd=tree, capture_output=True, text=True,
                              timeout=300, env=env)
        last = (done.stdout.strip().splitlines() or [""])[-1].split()
        ok = done.returncode == 0 and len(last) == 3 and last[:2] == ["200", "ok"]
        return {"ref": ref, "ok": ok, "login_status": last[0] if last else None,
                "visits_seen": int(last[2]) if ok else None,
                "error": None if ok else (done.stderr.strip().splitlines() or ["?"])[-1][:200]}
    finally:
        subprocess.run(["git", "-C", str(ROOT), "worktree", "remove", "--force", str(tree)],
                       capture_output=True)


def drill(data_root=ROOT, dest=None, key_path=None, new_backup=True, rollback_ref=None, now=None):
    import erasure
    import storage
    data_root = Path(data_root)
    dest = Path(dest or data_root / "backups")
    now = now or clinic_time.now_utc()
    report = {"started_utc": now.isoformat(), "problems": []}

    before = newest_archive(dest)
    report["rpo_h_at_start"] = (round((time.time() - before.stat().st_mtime) / 3600, 2)
                                if before else None)
    report["backup_schedule"] = "none installed"
    if new_backup:
        created = backup.create(data_root=data_root, dest=dest, key_path=key_path)
        archive = Path(created["archive"])
        report["backup_seconds"] = created["seconds"]
    else:
        archive = before
    if archive is None:
        report["problems"].append("no backup to restore")
        return report
    report["archive"] = archive.name

    target = pick_patient(data_root / backup.DB_REL)
    with tempfile.TemporaryDirectory(prefix="restore-drill-") as work:
        os.chmod(work, 0o700)
        stones = Path(work) / "tombstones.jsonl"
        live = Path(erasure.TOMBSTONES)
        text = live.read_text() if live.exists() else ""
        drill_stone = {"patient_id": target[0], "cf_hmac": [erasure.cf_mac(target[1])],
                       "request_id": "restore-drill", "erased_at": now.isoformat(), "held": []}
        stones.write_text(text + json.dumps(drill_stone) + "\n")

        into = Path(work) / "restored"
        started = time.monotonic()
        result = backup.restore(archive, into, key_path=key_path, apply=True, rebuild_index=True,
                                tombstones=stones)
        report["rto_seconds"] = round(time.monotonic() - started, 2)
        report["problems"] += result["problems"]
        report["rebuilt"] = {"indexed": result["rebuilt"]["indexed"],
                             "failed": len(result["rebuilt"]["failed"])} if result.get("rebuilt") else None
        report["reapplied_erasures"] = len(result.get("reapplied_erasures") or [])

        conn = storage.connect(str(into / backup.DB_REL))
        try:
            counts = result["manifest_counts"]
            # an invoiced visit stays as a dated shell under the retention hold
            # (erasure._sqlite); anything clinical left on it is a failure
            left = conn.execute("SELECT COUNT(*) FROM visits WHERE patient_id = ? AND (procedures != '[]'"
                                " OR clinical_notes != '' OR next_appointment IS NOT NULL"
                                " OR source_path NOT LIKE 'erased:%')", (target[0],)).fetchone()[0]
            shells = conn.execute("SELECT COUNT(*) FROM visits WHERE patient_id = ? AND source_path LIKE"
                                  " 'erased:%'", (target[0],)).fetchone()[0]
            others = conn.execute("SELECT COUNT(*) FROM visits WHERE patient_id != ?", (target[0],)).fetchone()[0]
            report["erasure_after_restore"] = {
                "drill_patient_visits_before": target[2], "drill_patient_visits_after": left,
                "invoice_hold_shells": shells,
                "notes_left": len(list((into / "sorted" / target[1].upper() / "notes").glob("*.json"))),
                "other_visits_kept": others == counts["visits"] - target[2]}
            if left or report["erasure_after_restore"]["notes_left"]:
                report["problems"].append("the erased patient is back after the restore")
            if not report["erasure_after_restore"]["other_visits_kept"]:
                report["problems"].append("the restore erased more than the named patient")
            report["integrity"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            conn.close()

        collection = storage.get_collection(str(into / backup.CHROMA_REL))
        ids = collection.get()["ids"]
        found = collection.query(query_texts=["dente"], n_results=1)["ids"][0] if ids else []
        report["search"] = {"chunks": len(ids), "answers": bool(found),
                            "drill_patient_chunks": sum(target[1].upper() in i.upper() for i in ids)}
        if report["rebuilt"] and report["rebuilt"]["indexed"] and not found:
            report["problems"].append("the rebuilt index does not answer a search")
        if report["search"]["drill_patient_chunks"]:
            report["problems"].append("the erased patient is still in the rebuilt index")

        if rollback_ref:
            report["rollback"] = rehearse_rollback(rollback_ref, into / backup.DB_REL, work)
            if not report["rollback"]["ok"]:
                report["problems"].append("rollback rehearsal failed")
    report["restored_copy_removed"] = True
    return report


def main(argv):
    if "--selftest" in argv:
        selftest()
        return 0
    ref = argv[argv.index("--rollback-ref") + 1] if "--rollback-ref" in argv else None
    report = drill(new_backup="--no-new-backup" not in argv, rollback_ref=ref)
    text = json.dumps(report, indent=1)
    if "--out" in argv:
        Path(argv[argv.index("--out") + 1]).write_text(text + "\n")
    print(text)
    return 1 if report["problems"] else 0


def selftest():
    import patient_id
    from storage import init_db
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "data"
        (root / "db").mkdir(parents=True)
        key = Path(tmp) / "backup.key"
        backup.init_key(key)
        os.environ["CLINIC_ERASURE_KEY_FILE"] = str(Path(tmp) / "erasure.key")
        conn = init_db(str(root / backup.DB_REL))
        keep = patient_id.seed_patient(conn, "ZZRD800101010101", "Keep Me")
        gone = patient_id.seed_patient(conn, "ZZRD800101010102", "Erase Me")
        for pid, cf, n in ((keep, "ZZRD800101010101", 1), (gone, "ZZRD800101010102", 2)):
            for i in range(n):
                src = f"{cf}/notes/v{i}.json"
                conn.execute("INSERT INTO visits (patient_id, visit_date, procedures, clinical_notes, source_path)"
                             " VALUES (?, ?, '[]', 'dente 36 controllo', ?)", (pid, f"2026-05-0{i + 1}", src))
                note = {"codice_fiscale": cf, "patient_name": "x", "visit_date": f"2026-05-0{i + 1}",
                        "procedures": [], "clinical_notes": "dente 36 controllo", "invoices": []}
                (root / "sorted" / cf / "notes").mkdir(parents=True, exist_ok=True)
                (root / "sorted" / src).write_text(json.dumps(note))
        # one of the erased patient's visits is invoiced: it must come back as a shell, nothing clinical
        vid = conn.execute("SELECT id FROM visits WHERE patient_id = ? ORDER BY id LIMIT 1", (gone,)).fetchone()[0]
        conn.execute("INSERT INTO invoices (patient_id, visit_id, line_index, amount, amount_cents, description)"
                     " VALUES (?, ?, 0, 50, 5000, 'x')", (gone, vid))
        conn.commit()
        conn.close()

        report = drill(data_root=root, key_path=key)
        assert report["problems"] == [], report["problems"]
        e = report["erasure_after_restore"]
        assert (e["drill_patient_visits_before"], e["drill_patient_visits_after"], e["notes_left"],
                e["invoice_hold_shells"]) == (2, 0, 0, 1), e
        assert e["other_visits_kept"] and report["reapplied_erasures"] == 1, report
        assert report["rto_seconds"] > 0 and report["integrity"] == "ok", report
        assert report["search"]["answers"] and report["search"]["drill_patient_chunks"] == 0, report["search"]
        assert report["rebuilt"] == {"indexed": 1, "failed": 0}, report["rebuilt"]
        assert report["rpo_h_at_start"] is None, "1: no earlier backup, so no RPO to report"
        again = drill(data_root=root, key_path=key, new_backup=False)
        assert again["rpo_h_at_start"] is not None and again["problems"] == [], again
        text = json.dumps(report)
        assert "Erase Me" not in text and "ZZRD800101010102" not in text, "the report names a patient"
        # the live tombstones were never written
        import erasure
        assert not Path(erasure.TOMBSTONES).exists() or "restore-drill" not in Path(erasure.TOMBSTONES).read_text()
        del os.environ["CLINIC_ERASURE_KEY_FILE"]
    print("selftest ok")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
