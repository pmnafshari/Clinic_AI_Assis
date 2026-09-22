"""The reminder pass (P09): one run, then exit.

Nothing schedules this. It is run by hand, or by a cron/launchd entry the owner
sets up; until one exists the reminders page says "schedule: not configured"
and shows when the job last ran.

The transport is disabled and there is no flag to enable it: no messaging
provider exists (D01, P11). A run therefore plans the queue, releases stale
claims and reports what is waiting, and sends nothing. Running it twice, or
twice at once, queues no second reminder for the same thing.

    python reminder_job.py                                   run now
    python reminder_job.py --now 2026-09-23T06:00:00+00:00   controlled clock
"""
import json
import sys

import clinic_time
import reminders
import storage

DB_PATH = "db/clinic.sqlite"
SCHEDULE = "not configured"


def main(argv):
    if "--selftest" in argv:
        selftest()
        return 0
    now = clinic_time.read_instant(argv[argv.index("--now") + 1]) if "--now" in argv else None
    conn = storage.init_db(DB_PATH)
    try:
        result = reminders.run_job(conn, now=now)
    finally:
        conn.close()
    result["schedule"] = SCHEDULE
    print(json.dumps(result, indent=2))
    return 0


def selftest():
    import subprocess
    import tempfile
    from pathlib import Path

    # the job as a separate process against its own database, with the clock it
    # is given - the same way a scheduler would run it
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "db" / "clinic.sqlite"
        db.parent.mkdir()
        storage.init_db(str(db)).close()
        script = Path(__file__).resolve()
        runs = []
        for _ in range(2):
            out = subprocess.run([sys.executable, str(script), "--now",
                                  "2026-09-23T06:00:00+00:00"], cwd=tmp, capture_output=True,
                                 text=True, env={"PYTHONPATH": str(script.parent)})
            assert out.returncode == 0, out.stderr
            runs.append(json.loads(out.stdout))
        assert all(r["transport"] == "disabled" for r in runs), "nothing is sent"
        assert all(r["sent"] == 0 for r in runs), f"nothing is sent: {runs}"
        assert runs[0]["schedule"] == "not configured"
        assert "reason" in runs[0], "the run says why it sent nothing"
        conn = storage.connect(str(db))
        started = [r[0] for r in conn.execute("SELECT started_at FROM reminder_job_runs")]
        statuses = [r[0] for r in conn.execute("SELECT status FROM reminder_job_runs")]
        conn.close()
        assert started == ["2026-09-23T06:00:00+00:00"] * 2, f"the clock given is the clock used: {started}"
        assert statuses == ["ok", "ok"], statuses
    print("selftest ok")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
