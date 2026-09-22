"""P10 T5 - the patient agent's action layer, scored in Italian and English.

THE THRESHOLD IS 1.0, AND IT IS FIXED HERE BEFORE THE RUN.

That is not ambition. The action layer is deterministic - regular expressions
and a date parser, no model, no sampling - so it has no variance to average
over. A case that fails is a defect in the router, not noise to tune around,
and lowering this number would be the same as deleting the case. The chat
ANSWER path has its own eval with its own threshold (`eval_chat.py`); this one
scores only what P10 added.

    .venv/bin/python eval_agent.py [--json]

No servers and no Ollama: the whole path under test is local and deterministic.
"""
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import clinic_time
import handoff
import patient_agent
import patient_id
from storage import init_db

THRESHOLD = 1.0
CASES_PATH = Path(__file__).resolve().parent / "eval_agent_cases.json"
AGENT_VERSION = "P10.1"


def _seed(conn, now):
    pid = patient_id.seed_patient(conn, "ZZV00A00A000A", "Valentina Eval", "+39 055 1")
    mallory = patient_id.seed_patient(conn, "ZZV00B00B000B", "Mallory Eval", None)
    stored = clinic_time.to_storage(clinic_time.to_utc(datetime(2026, 10, 1, 9, 0)))
    theirs = conn.execute(
        "INSERT INTO appointments (patient_id, dentist, starts_at, minutes, status,"
        " created_at, updated_at) VALUES (?, 'drossi', ?, 30, 'booked', ?, ?)",
        (mallory, stored, stored, stored)).lastrowid
    conn.commit()
    return pid, mallory, theirs


def _give_booking(conn, pid):
    stored = clinic_time.to_storage(clinic_time.to_utc(datetime(2026, 10, 2, 9, 0)))
    row = conn.execute(
        "INSERT INTO appointments (patient_id, dentist, starts_at, minutes, status,"
        " created_at, updated_at) VALUES (?, 'drossi', ?, 30, 'booked', ?, ?)",
        (pid, stored, stored, stored)).lastrowid
    conn.commit()
    return row


def _reset(conn, pid):
    conn.execute("DELETE FROM patient_agent_actions WHERE patient_id = ?", (pid,))
    conn.execute("DELETE FROM handoff_requests WHERE patient_id = ?", (pid,))
    conn.execute("UPDATE appointments SET status = 'cancelled' WHERE patient_id = ?", (pid,))
    conn.commit()


def run():
    data = json.loads(CASES_PATH.read_text())
    now = clinic_time.read_instant("2026-09-22T08:00:00+00:00")
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        conn = init_db(str(Path(tmp) / "clinic.sqlite"))
        pid, _mallory, theirs = _seed(conn, now)
        for case in data["cases"]:
            _reset(conn, pid)
            mine = _give_booking(conn, pid) if case.get("needs_booking") else None
            if case.get("needs_open"):
                patient_agent.handle(conn, pid, "vorrei prenotare il 24/10 mattina", now=now)
            out = patient_agent.handle(conn, pid, case["say"], case["lang"], now=now)
            got = "fallthrough" if out is None else out["state"]
            ok = True
            detail = got
            if case["expect"] == "not_other_patient":
                # the only thing that matters: no proposal may ever name an
                # appointment that is not this patient's own
                proposed = (out or {}).get("payload", {}).get("appointment_id")
                ok = proposed != theirs
                detail = f"proposed={proposed} theirs={theirs} state={got}"
            else:
                ok = got == case["expect"]
                if ok and case.get("need"):
                    ok = out.get("need") == case["need"]
                    detail = f"{got}/{out.get('need')}"
                if ok and case.get("kind"):
                    ok = out.get("kind") == case["kind"]
                    detail = f"{got}/{out.get('kind')}"
            results.append({"id": case["id"], "lang": case["lang"], "pass": ok,
                            "expected": case["expect"], "got": detail})
        # the appointment that was never this patient's must be untouched by
        # every case above, injections included
        survived = conn.execute("SELECT status FROM appointments WHERE id = ?",
                                (theirs,)).fetchone()[0] == "booked"
        conn.close()
    return data, results, survived


def main():
    data, results, survived = run()
    as_json = "--json" in sys.argv
    by_lang = {}
    for r in results:
        got = by_lang.setdefault(r["lang"], [0, 0])
        got[1] += 1
        got[0] += 1 if r["pass"] else 0
    passed = sum(1 for r in results if r["pass"])
    rate = passed / len(results) if results else 0.0
    report = {
        "dataset_version": data["dataset_version"],
        "agent_version": AGENT_VERSION,
        "threshold": THRESHOLD,
        "cases": len(results),
        "passed": passed,
        "rate": round(rate, 4),
        "by_language": {k: {"passed": v[0], "of": v[1]} for k, v in sorted(by_lang.items())},
        "other_patient_appointment_untouched": survived,
        "failures": [r for r in results if not r["pass"]],
    }
    if as_json:
        print(json.dumps(report, indent=2))
    else:
        for r in results:
            if not r["pass"]:
                print(f"FAIL {r['id']} ({r['lang']}) expected {r['expected']}, got {r['got']}")
        for lang, v in sorted(by_lang.items()):
            print(f"{lang}: {v[0]}/{v[1]}")
        print(f"dataset {data['dataset_version']} · agent {AGENT_VERSION} · "
              f"threshold {THRESHOLD}")
        print(f"rate {rate:.4f} over {len(results)} cases")
        print(f"another patient's appointment untouched: {survived}")
    if not survived:
        print("FAILED: an injection reached another patient's appointment")
        return 1
    if rate < THRESHOLD:
        print(f"FAILED: {rate:.4f} is below the threshold {THRESHOLD}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
