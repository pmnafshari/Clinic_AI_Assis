"""operations check for the demo install (P17.03). counts, ages and yes/no only -
never a name, codice fiscale, phone, note or file name, so the output can go
anywhere an alert goes.

    .venv/bin/python health.py            # human-readable, exit 1 on any alert
    .venv/bin/python health.py --json
    .venv/bin/python health.py --selftest

an alert is also appended to db/alerts.log (utc time and check names only).
there is no approved person to send alerts to (D11), so nothing is sent and
nothing is scheduled: run it by hand, or from a job the owner installs.

what it does not measure: model eval drift between gate runs (the evals are run
by hand in Gate G), provider latency (no provider is connected), load.
"""

import json
import shutil
import sqlite3
import sys
import time
import urllib.request
from datetime import timedelta
from pathlib import Path

import clinic_time

ROOT = Path(__file__).resolve().parent
APPS = (("staff", "http://127.0.0.1:5000/login"), ("patient", "http://127.0.0.1:5001/login"),
        ("site", "http://127.0.0.1:5002/"), ("model", "http://127.0.0.1:11434/api/tags"))
SLOW_MS = 2000
MIN_FREE_BYTES = 2 * 1024 ** 3
MAX_BACKUP_AGE = timedelta(hours=26)       # daily backup plus slack
MAX_INBOX_AGE = timedelta(minutes=15)      # the watcher files a drop in seconds
MAX_CLAIM_AGE = timedelta(minutes=30)      # reminders.release_stale frees these anyway
ALERT_LOG = "db/alerts.log"


def probe(url, timeout=3):
    """-> (reachable, milliseconds). loopback only; no proxy."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    started = time.monotonic()
    try:
        with opener.open(url, timeout=timeout) as r:
            ok = r.status < 500
    except Exception:
        return False, None
    return ok, round((time.monotonic() - started) * 1000)


def filevault():
    import disk_guard
    return disk_guard.filevault_state(disk_guard.read_status())


def _age(now, stamp):
    return now - clinic_time.read_instant(stamp) if stamp else None


def _hours(delta):
    return None if delta is None else round(delta.total_seconds() / 3600, 1)


def collect(conn, root=ROOT, now=None, http=probe, fv=filevault):
    """-> [(check, value, alert or None)]"""
    now = now or clinic_time.now_utc()
    root = Path(root)
    out = []

    for name, url in APPS:
        up, ms = http(url)
        alert = f"{name} not reachable" if not up else (f"{name} slow ({ms} ms)" if ms > SLOW_MS else None)
        out.append((f"app.{name}", {"up": up, "ms": ms}, alert))

    free = shutil.disk_usage(root).free
    out.append(("disk.free_gib", round(free / 1024 ** 3, 1),
                "low disk space" if free < MIN_FREE_BYTES else None))
    state = fv()
    out.append(("disk.filevault", state, None if state == "on" else f"filevault {state}"))

    archives = sorted((root / "backups").glob("clinic-*.cbk"), key=lambda p: p.stat().st_mtime)
    if not archives:
        out.append(("backup.newest_age_h", None, "no backup"))
    else:
        age = now.timestamp() - archives[-1].stat().st_mtime
        out.append(("backup.newest_age_h", round(age / 3600, 1),
                    "backup older than 26 h" if age > MAX_BACKUP_AGE.total_seconds() else None))

    # machine queues: these should drain on their own, so age is lag
    inbox = [p for p in (root / "drop").rglob("*") if p.is_file()] if (root / "drop").exists() else []
    oldest = max((now.timestamp() - p.stat().st_mtime for p in inbox), default=0)
    out.append(("queue.inbox_files", {"count": len(inbox), "oldest_min": round(oldest / 60)},
                "inbox not draining" if oldest > MAX_INBOX_AGE.total_seconds() else None))
    stuck = conn.execute("SELECT COUNT(*) FROM reminder_jobs WHERE status = 'claimed' AND claimed_at < ?",
                         ((now - MAX_CLAIM_AGE).isoformat(),)).fetchone()[0]
    out.append(("queue.reminder_claims_stuck", stuck, "reminder jobs stuck in claim" if stuck else None))
    failed = conn.execute("SELECT COUNT(*) FROM reminder_jobs WHERE status = 'failed'").fetchone()[0]
    out.append(("queue.reminder_failed", failed, "reminder jobs failed" if failed else None))
    run = conn.execute("SELECT * FROM reminder_job_runs ORDER BY id DESC LIMIT 1").fetchone()
    out.append(("queue.reminder_last_run_h", _hours(_age(now, run["started_at"])) if run else None, None))
    temps = [p for p in (root / "documents").rglob(".upload-*")] if (root / "documents").exists() else []
    out.append(("queue.document_temp_files", len(temps),
                "unreconciled document temps (documents.py --reconcile)" if temps else None))

    # people's queues: they wait on a person by design, so they are reported, not alerted
    for check, sql in (
            ("people.notes_awaiting_review", "SELECT COUNT(*), MIN(created_at) FROM note_reviews WHERE status = 'pending'"),
            ("people.documents_awaiting_review", "SELECT COUNT(*), MIN(uploaded_at) FROM patient_documents WHERE status = 'pending_review'"),
            ("people.callbacks_open", "SELECT COUNT(*), MIN(created_at) FROM handoff_requests WHERE status IN ('open', 'claimed')")):
        n, first = conn.execute(sql).fetchone()
        out.append((check, {"count": n, "oldest_h": _hours(_age(now, first))}, None))

    import providers
    for kind in sorted(providers.ENV_ENABLED):
        row = conn.execute("SELECT killed, spend_cap_cents, spent_cents FROM provider_switches WHERE kind = ?",
                           (kind,)).fetchone()
        killed, cap, spent = (bool(row["killed"]), row["spend_cap_cents"], row["spent_cents"]) if row else (False, None, 0)
        over = bool(cap) and spent > cap
        out.append((f"provider.{kind}", {"enabled": providers.feature_enabled(kind), "killed": killed,
                                         "spent_cents": spent, "cap_cents": cap},
                    f"{kind} over its spend cap" if over else None))
    unknown = conn.execute("SELECT COUNT(*) FROM delivery_receipts WHERE outcome = 'unknown'").fetchone()[0]
    out.append(("provider.deliveries_unresolved", unknown, "deliveries with unknown outcome" if unknown else None))
    return out


def alerts(findings):
    return [a for _c, _v, a in findings if a]


def write_alert(root, findings, now=None):
    now = now or clinic_time.now_utc()
    names = [c for c, _v, a in findings if a]
    path = Path(root) / ALERT_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(f"{now.isoformat()} | {len(names)} alert(s) | {', '.join(names)}\n")


def render(findings):
    lines = []
    for check, value, alert in findings:
        lines.append(f"{'ALERT' if alert else 'ok   '}  {check:<34} {json.dumps(value)}"
                     + (f"  <- {alert}" if alert else ""))
    n = len(alerts(findings))
    lines.append(f"{n} alert(s)" if n else "no alerts")
    return "\n".join(lines)


def main(argv):
    if "--selftest" in argv:
        selftest()
        return 0
    conn = sqlite3.connect(ROOT / "db" / "clinic.sqlite")
    conn.row_factory = sqlite3.Row
    try:
        findings = collect(conn)
    finally:
        conn.close()
    if "--json" in argv:
        print(json.dumps([{"check": c, "value": v, "alert": a} for c, v, a in findings], indent=1))
    else:
        print(render(findings))
    if alerts(findings):
        write_alert(ROOT, findings)
        return 1
    return 0


def selftest():
    import os
    import re
    import tempfile

    import patient_id
    from storage import init_db

    now = clinic_time.read_instant("2026-09-17T10:00:00+00:00")
    up = lambda url: (True, 40)                                       # noqa: E731
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "db").mkdir()
        conn = init_db(str(root / "db" / "clinic.sqlite"))
        pid = patient_id.seed_patient(conn, "ZZHL800101010101", "Hilda Health")
        (root / "backups").mkdir()
        fresh = root / "backups" / "clinic-20260917T090000Z.cbk"
        fresh.write_bytes(b"x")
        os.utime(fresh, (now.timestamp() - 3600, now.timestamp() - 3600))
        conn.execute("INSERT INTO note_reviews (origin, status, patient_id, codice_fiscale, original_name,"
                     " created_by, created_at) VALUES ('upload', 'pending', ?, 'ZZHL800101010101',"
                     " 'hilda-note.txt', 'dentist', ?)", (pid, (now - timedelta(days=2)).isoformat()))
        conn.commit()

        def run(**kw):
            return collect(conn, root, now, **{"http": up, "fv": lambda: "on", **kw})

        # 1. a healthy install: no alert, and the people's queue is reported, not alerted
        f = run()
        assert alerts(f) == [], alerts(f)
        people = dict((c, v) for c, v, _a in f)["people.notes_awaiting_review"]
        assert people == {"count": 1, "oldest_h": 48.0}, people

        # 2. nothing identifying in the output, whatever the data holds
        text = render(f) + json.dumps([v for _c, v, _a in f])
        for secret in ("Hilda", "ZZHL800101010101", "hilda-note"):
            assert secret not in text, f"2: {secret} leaked"
        assert not re.search(r"[A-Z]{4}[0-9]{12}", text), "2: a codice fiscale shape in the output"

        # 3. each failure raises its own alert
        assert "staff not reachable" in alerts(run(http=lambda u: (False, None) if ":5000" in u else (True, 40)))
        assert "site slow (2500 ms)" in alerts(run(http=lambda u: (True, 2500) if ":5002" in u else (True, 40)))
        assert "filevault off" in alerts(run(fv=lambda: "off"))
        assert "filevault unknown" in alerts(run(fv=lambda: "unknown")), "3: unknown is not fine"
        os.utime(fresh, (now.timestamp() - 30 * 3600, now.timestamp() - 30 * 3600))
        assert "backup older than 26 h" in alerts(run()), "3: a stale backup must alert"
        fresh.unlink()
        assert "no backup" in alerts(run())
        (root / "backups" / "clinic-20260917T095000Z.cbk").write_bytes(b"x")
        os.utime(root / "backups" / "clinic-20260917T095000Z.cbk", (now.timestamp(), now.timestamp()))

        (root / "drop").mkdir()
        waiting = root / "drop" / "note.txt"
        waiting.write_text("x")
        os.utime(waiting, (now.timestamp() - 60, now.timestamp() - 60))
        assert alerts(run()) == [], "3: a minute-old drop is normal"
        os.utime(waiting, (now.timestamp() - 3600, now.timestamp() - 3600))
        assert "inbox not draining" in alerts(run()), "3: worker lag must alert"
        waiting.unlink()

        conn.execute("INSERT INTO reminder_jobs (kind, patient_id, subject_id, version, idempotency_key,"
                     " send_at, status, claimed_by, claimed_at, created_at) VALUES ('appointment', ?, 1, 'v',"
                     " 'k1', ?, 'claimed', 'w', ?, ?)",
                     (pid, now.isoformat(), (now - timedelta(hours=2)).isoformat(), now.isoformat()))
        conn.commit()
        assert "reminder jobs stuck in claim" in alerts(run())
        conn.execute("UPDATE reminder_jobs SET status = 'failed'")
        conn.commit()
        assert "reminder jobs failed" in alerts(run())
        conn.execute("DELETE FROM reminder_jobs")
        conn.execute("INSERT INTO provider_switches (kind, killed, spend_cap_cents, spent_cents)"
                     " VALUES ('messaging', 0, 100, 150)")
        conn.commit()
        assert "messaging over its spend cap" in alerts(run())
        conn.execute("UPDATE provider_switches SET spend_cap_cents = 0, spent_cents = 0")
        conn.commit()

        # 4. an alert run appends one PHI-free line and exits 1 through main's path
        f = run(fv=lambda: "off")
        write_alert(root, f, now)
        line = (root / ALERT_LOG).read_text()
        assert line.strip().endswith("disk.filevault") and "Hilda" not in line, line
        conn.close()
    print("selftest ok")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
