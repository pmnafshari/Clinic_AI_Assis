"""one support period (P21.02, P21.05). run it by hand, or from a schedule the owner installs
(templates in docs/support/); nothing here installs a schedule or sends anything.

    .venv/bin/python period_run.py run [--no-drill]
    .venv/bin/python period_run.py report [--matrix ../new_steps_approach.md]
    .venv/bin/python period_run.py --selftest

a run: one health sample (kept for availability), a backup with its verify, a
restore drill on that backup, and the fixed deterministic eval (eval_agent). the
result is compared with the previous period; a metric that dropped, a failed
drill or a failed backup opens a support ticket that says what to do. the
record is written to db/ops/period-<utc stamp>.json.

the report reads those records and the health samples: availability, tickets,
restore age, error budget (none: no SLO is approved), eval metrics, cost, and
progress percentages counted from the checkboxes of the steps matrix.
"""

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import clinic_time
import support

ROOT = Path(__file__).resolve().parent
OPS = ROOT / "db" / "ops"


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _previous(ops, stamp):
    older = sorted(p for p in Path(ops).glob("period-*.json") if p.stem.split("-", 1)[1] < stamp)
    return json.loads(older[-1].read_text()) if older else None


def run(conn, ops=OPS, health=None, steps=None, stamp=None):
    import health as health_mod
    ops = Path(ops)
    ops.mkdir(parents=True, exist_ok=True)
    stamp = stamp or _stamp()
    health = health or health_mod.collect
    steps = real_steps() if steps is None else steps

    findings = health(conn)
    apps = [v for c, v, _a in findings if c.startswith("app.")]
    all_up = bool(apps) and all(v["up"] for v in apps)
    alerts = [a for _c, _v, a in findings if a]
    conn.execute("INSERT INTO ops_health_samples (at, all_up, alerts, checks) VALUES (?, ?, ?, ?)",
                 (clinic_time.stamp(), int(all_up), len(alerts), json.dumps([c for c, _v, a in findings if a])))
    conn.commit()
    record = {"stamp": stamp, "health": {"all_up": all_up, "alerts": len(alerts)}, "drops": [], "tickets_opened": []}

    for name in ("backup", "drill", "evals"):
        if name in steps:
            try:
                record[name] = steps[name]()
            except Exception as e:             # a step that breaks is a finding, not a crash of the run
                record[name] = {"error": f"{type(e).__name__}: {e}"[:200]}

    def ticket(summary, severity, source):
        record["tickets_opened"].append(support.open_ticket(conn, summary, severity, "system", "system", source=source))

    if record.get("backup", {}).get("problems") or "error" in record.get("backup", {}):
        ticket("backup failed in the period run: re-run backup.py create and verify before anything else", "high", "drill")
    drill = record.get("drill", {})
    if drill.get("problems") or "error" in drill:
        what = "; ".join(drill.get("problems") or [drill.get("error", "")])[:150]
        ticket(f"restore drill failed: {what} - stop and restore-test by hand (docs/backup-runbook.md)", "high", "drill")

    prev = _previous(ops, stamp)
    if prev and isinstance(prev.get("evals"), dict) and isinstance(record.get("evals"), dict):
        for metric, after in record["evals"].items():
            before = prev["evals"].get(metric)
            if isinstance(after, (int, float)) and isinstance(before, (int, float)) and after < before:
                record["drops"].append({"metric": metric, "before": before, "after": after})
                ticket(f"metric drop {metric} {before} -> {after}: re-run the eval, compare with the last release, "
                       f"and roll back or open a fix before the next period", "high", "eval")

    (ops / f"period-{stamp}.json").write_text(json.dumps(record, indent=1) + "\n")
    return record


def real_steps(drill=True):
    import backup

    def do_backup():
        made = backup.create()
        _manifest, problems = backup.verify(made["archive"])
        return {"archive": Path(made["archive"]).name, "problems": problems}

    def do_drill():
        import restore_drill
        rep = restore_drill.drill(new_backup=False)
        return {"problems": rep["problems"], "rto_seconds": rep.get("rto_seconds"),
                "rpo_h_at_start": rep.get("rpo_h_at_start")}

    def do_evals():
        out = subprocess.run([sys.executable, "eval_agent.py", "--json"], capture_output=True, text=True,
                             cwd=ROOT, timeout=600)
        data = json.loads(out.stdout)
        return {"agent_rate": data["rate"], "agent_cases": data["cases"]}

    steps = {"backup": do_backup, "evals": do_evals}
    if drill:
        steps["drill"] = do_drill
    return steps


def progress(matrix):
    counts, phase = {}, None
    for line in Path(matrix).read_text().splitlines():
        head = re.match(r"^## (P\d{2})\b", line)
        if head:
            phase = head.group(1)
        elif line.startswith("## "):
            phase = None
        elif phase and re.match(r"^- \[[ x]\] ", line):
            c = counts.setdefault(phase, {"done": 0, "of": 0})
            c["of"] += 1
            c["done"] += line.startswith("- [x]")
    return {p: {**c, "pct": round(100 * c["done"] / c["of"], 1)} for p, c in counts.items()}


def report(conn, ops=OPS, matrix=None):
    samples = conn.execute("SELECT COUNT(*), COALESCE(SUM(all_up), 0) FROM ops_health_samples").fetchone()
    records = [json.loads(p.read_text()) for p in sorted(Path(ops).glob("period-*.json"))]
    drills = [r for r in records if isinstance(r.get("drill"), dict) and not r["drill"].get("problems")
              and "error" not in r["drill"]]
    restore_age = None
    if drills:
        last = datetime.strptime(drills[-1]["stamp"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        restore_age = round((clinic_time.now_utc() - last).total_seconds() / 3600, 1)
    cost = conn.execute("SELECT COALESCE(SUM(spent_cents), 0) FROM provider_switches").fetchone()[0]
    open_n = conn.execute("SELECT COUNT(*) FROM support_tickets WHERE status != 'closed'").fetchone()[0]
    closed_n = conn.execute("SELECT COUNT(*) FROM support_tickets WHERE status = 'closed'").fetchone()[0]
    return {
        "periods": len(records),
        "availability": {"samples": samples[0], "all_up": samples[1],
                         "rate": round(samples[1] / samples[0], 4) if samples[0] else None},
        "tickets": {"open": open_n, "closed": closed_n},
        "restore_age_h": restore_age,
        "error_budget": "no SLO approved",
        "model_quality": records[-1].get("evals") if records else None,
        "drops_last_period": records[-1].get("drops") if records else None,
        "cost_cents": cost,
        "progress": progress(matrix) if matrix else "not measured (no matrix given)",
        "programme": "ACTIVE",
    }


def main(argv):
    if "--selftest" in argv:
        import support_selftest
        support_selftest.selftest()
        return 0
    import sqlite3
    conn = sqlite3.connect(ROOT / "db" / "clinic.sqlite")
    conn.row_factory = sqlite3.Row
    try:
        if argv[:1] == ["run"]:
            rec = run(conn, steps=real_steps(drill="--no-drill" not in argv))
            print(json.dumps(rec, indent=1))
            return 1 if rec["tickets_opened"] else 0
        if argv[:1] == ["report"]:
            matrix = argv[argv.index("--matrix") + 1] if "--matrix" in argv else None
            print(json.dumps(report(conn, matrix=matrix), indent=1))
            return 0
    finally:
        conn.close()
    print(__doc__.split("\n\n")[1])
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
