"""the pilot's machinery, on the non-clinical X-ray demo (P20). NOT a pilot.

a real pilot needs approvals nobody has given (P20.01) and real cases; this is
the set of controls such a pilot would run inside, exercised on the P19
synthetic demo so they are proven before anyone relies on them:

- a job queue in front of the demo, with a kill switch: any staff member can
  stop it (stopping is always safe), queued work is cancelled, nothing new is
  accepted; only a dentist can resume.
- mandatory review: output leaves the queue (released()) only after a dentist
  accepted or corrected it. rejected or unreviewed output never does. no output
  is ever written to a patient record.
- incidents: a reject or correct can carry a false-negative / false-positive
  incident, which is escalated as a PHI-free alert line until a dentist
  acknowledges it.
- model change policy: each detector version runs only after it passed a
  revalidation on the locked benchmark; rollback is choosing an earlier
  validated version. every job keeps its image, its version and its result, so
  any historical result can be reproduced.
- drift: the recent window's abstention rate and marks per image against the
  benchmark baseline.
- a report of all of it, with the continue / fix / stop decision left empty for
  the owner and the clinical owner.

everything needs the demo switch (CLINIC_XRAY_DEMO=1 and a valid DEMO_GO record);
a broken gate record turns it off.
"""

import hashlib
import json
from pathlib import Path

import clinic_time
import xray_demo as xd
import xray_gate
from xray_demo import Refused

LABEL = ("DEMO PILOT MACHINERY - synthetic images, demo marks only. Not a clinical pilot, not a finding, "
         "not a diagnosis.")
DETECTORS = {
    xd.DETECTOR: xd.detect,
    # the earlier rule (smaller minimum mark), kept so rollback has somewhere validated to go
    "demo-marks-threshold-0": lambda pixels: xd.detect(pixels, min_area=4),
}
INCIDENT_KINDS = ("false_negative", "false_positive", "other")
DRIFT_WINDOW = 20
DRIFT_ABSTENTION = 0.2          # absolute change in the abstention rate
DRIFT_MARKS = 1.0               # absolute change in marks per image
ALERT_LOG = Path("db/alerts.log")

SCHEMA = """
CREATE TABLE IF NOT EXISTS xray_demo_control (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    killed INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    active_version TEXT NOT NULL,
    updated_by TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS xray_demo_validations (
    id INTEGER PRIMARY KEY,
    version TEXT NOT NULL,
    criteria_sha256 TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    passed INTEGER NOT NULL,
    report TEXT NOT NULL,
    validated_by TEXT NOT NULL,
    validated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS xray_demo_jobs (
    id INTEGER PRIMARY KEY,
    image BLOB NOT NULL,
    input_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'result', 'abstained', 'cancelled')),
    detector TEXT,
    result TEXT,
    reason TEXT,
    review_id INTEGER,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS xray_demo_incidents (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('false_negative', 'false_positive', 'other')),
    status TEXT NOT NULL CHECK (status IN ('open', 'acknowledged')),
    raised_by TEXT NOT NULL,
    raised_at TEXT NOT NULL,
    acknowledged_by TEXT,
    acknowledged_at TEXT
);
CREATE TRIGGER IF NOT EXISTS xray_demo_validations_no_update BEFORE UPDATE ON xray_demo_validations
    BEGIN SELECT RAISE(ABORT, 'xray demo validations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS xray_demo_validations_no_delete BEFORE DELETE ON xray_demo_validations
    BEGIN SELECT RAISE(ABORT, 'xray demo validations are append-only'); END;
"""


def _audit(conn, actor, role, action, target, allowed, reason=None):
    from auth import log_audit
    log_audit(conn, actor, role, action, target, allowed=allowed, reason=reason)


def _require(conn, actor, role, permission, action, target):
    from auth import authorize
    if not authorize(role, permission):
        _audit(conn, actor, role, action, target, 0)
        raise Refused(f"{role} is not allowed to {action.replace('xray_demo_', '').replace('_', ' ')}")


def _control(conn):
    row = conn.execute("SELECT * FROM xray_demo_control WHERE id = 1").fetchone()
    if row is None:
        conn.execute("INSERT INTO xray_demo_control (id, killed, active_version) VALUES (1, 0, ?)", (xd.DETECTOR,))
        conn.commit()
        row = conn.execute("SELECT * FROM xray_demo_control WHERE id = 1").fetchone()
    return row


def _switched_on(env, gate):
    if not xray_gate.demo_enabled(gate, env):
        raise Refused("the X-ray demo is switched off (CLINIC_XRAY_DEMO=1 and a valid DEMO_GO in docs/xray/gate.json)")


# --- kill switch -----------------------------------------------------------------


def kill(conn, actor, role, reason):
    _require(conn, actor, role, "stop_xray_demo", "xray_demo_kill", "demo:control")
    _control(conn)
    conn.execute("UPDATE xray_demo_control SET killed = 1, reason = ?, updated_by = ?, updated_at = ? WHERE id = 1",
                 (reason, actor, clinic_time.stamp()))
    conn.commit()
    _audit(conn, actor, role, "xray_demo_kill", "demo:control", 1, reason)


def resume(conn, actor, role):
    _require(conn, actor, role, "manage_xray_demo", "xray_demo_resume", "demo:control")
    _control(conn)
    conn.execute("UPDATE xray_demo_control SET killed = 0, reason = NULL, updated_by = ?, updated_at = ? WHERE id = 1",
                 (actor, clinic_time.stamp()))
    conn.commit()
    _audit(conn, actor, role, "xray_demo_resume", "demo:control", 1)


# --- versions ------------------------------------------------------------------------


def validated(conn, version):
    return conn.execute("SELECT 1 FROM xray_demo_validations WHERE version = ? AND passed = 1",
                        (version,)).fetchone() is not None


def revalidate(conn, version, bench, criteria_path, locked, actor, role, env=None):
    _require(conn, actor, role, "manage_xray_demo", "xray_demo_revalidate", f"demo:{version}")
    if version not in DETECTORS:
        raise Refused(f"unknown detector version {version}")
    report = xd.evaluate(bench, criteria_path, locked, env=env, detector=DETECTORS[version], detector_name=version)
    passed = all(k["meets_criteria"] for k in report["per_kind"].values())
    conn.execute("INSERT INTO xray_demo_validations (version, criteria_sha256, manifest_sha256, passed, report, validated_by, validated_at)"
                 " VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (version, locked["criteria_sha256"], locked["manifest_sha256"], int(passed),
                  json.dumps(report), actor, clinic_time.stamp()))
    conn.commit()
    _audit(conn, actor, role, "xray_demo_revalidate", f"demo:{version}", 1, "passed" if passed else "failed")
    return {"version": version, "passed": passed}


def set_version(conn, version, actor, role):
    _require(conn, actor, role, "manage_xray_demo", "xray_demo_set_version", f"demo:{version}")
    if version not in DETECTORS or not validated(conn, version):
        raise Refused(f"{version} is not validated on the locked benchmark")
    _control(conn)
    conn.execute("UPDATE xray_demo_control SET active_version = ?, updated_by = ?, updated_at = ? WHERE id = 1",
                 (version, actor, clinic_time.stamp()))
    conn.commit()
    _audit(conn, actor, role, "xray_demo_set_version", f"demo:{version}", 1)


# --- queue ------------------------------------------------------------------------------


def enqueue(conn, image, actor, role, env=None, gate=None):
    _switched_on(env, gate)
    _require(conn, actor, role, "review_xray_demo", "xray_demo_enqueue", "demo:queue")
    if _control(conn)["killed"]:
        raise Refused("the demo is stopped by the kill switch")
    meta = xd.validate(image)
    jid = conn.execute("INSERT INTO xray_demo_jobs (image, input_sha256, status, created_by, created_at)"
                       " VALUES (?, ?, 'queued', ?, ?)", (image, meta["sha256"], actor, clinic_time.stamp())).lastrowid
    conn.commit()
    return jid


def run_queue(conn, env=None, gate=None):
    """process queued jobs one at a time, checking the kill switch before each."""
    _switched_on(env, gate)
    done = {"processed": 0, "cancelled": 0}
    for (jid,) in conn.execute("SELECT id FROM xray_demo_jobs WHERE status = 'queued' ORDER BY id").fetchall():
        control = _control(conn)
        if control["killed"]:
            conn.execute("UPDATE xray_demo_jobs SET status = 'cancelled', reason = ?, finished_at = ? WHERE id = ?",
                         ("stopped by the kill switch", clinic_time.stamp(), jid))
            conn.commit()
            done["cancelled"] += 1
            continue
        version = control["active_version"]
        if not validated(conn, version):
            raise Refused(f"{version} is not validated on the locked benchmark")
        image = conn.execute("SELECT image FROM xray_demo_jobs WHERE id = ?", (jid,)).fetchone()[0]
        result = xd.analyse(image, env=env, detector=DETECTORS[version])
        conn.execute("UPDATE xray_demo_jobs SET status = ?, detector = ?, result = ?, reason = ?, finished_at = ?"
                     " WHERE id = ?", (result["status"], version, json.dumps(result["marks"]), result["reason"],
                                       clinic_time.stamp(), jid))
        conn.commit()
        done["processed"] += 1
    return done


def job(conn, jid):
    row = conn.execute("SELECT * FROM xray_demo_jobs WHERE id = ?", (jid,)).fetchone()
    return dict(row) if row else None


def jobs(conn):
    return [dict(r) for r in conn.execute("SELECT id, input_sha256, status, detector, result, reason, review_id"
                                          " FROM xray_demo_jobs ORDER BY id")]


# --- review, release, incidents --------------------------------------------------------


def review(conn, jid, decision, actor, role, corrected=None, incident=None, alert_log=None):
    j = job(conn, jid)
    if j is None or j["status"] not in ("result", "abstained"):
        raise Refused("only a finished job can be reviewed")
    if j["review_id"]:
        raise Refused("this job is already reviewed; reviews are not overwritten")
    analysis = {"input_sha256": j["input_sha256"], "generator": xd.GENERATOR, "detector": j["detector"],
                "marks": json.loads(j["result"])}
    rid = xd.record_review(conn, analysis, decision, actor, role, corrected=corrected)
    conn.execute("UPDATE xray_demo_jobs SET review_id = ? WHERE id = ?", (rid, jid))
    conn.commit()
    if incident:
        if incident not in INCIDENT_KINDS or decision == "accept":
            raise Refused("an incident goes with a reject or a correct, of a known kind")
        conn.execute("INSERT INTO xray_demo_incidents (job_id, kind, status, raised_by, raised_at)"
                     " VALUES (?, ?, 'open', ?, ?)", (jid, incident, actor, clinic_time.stamp()))
        conn.commit()
        _audit(conn, actor, role, "xray_demo_incident", f"demo-job:{jid}", 1, incident)
        escalate(conn, alert_log)
    return rid


def escalate(conn, alert_log=None):
    """one PHI-free line per escalation, listing the open incident kinds and counts."""
    rows = conn.execute("SELECT kind, COUNT(*) FROM xray_demo_incidents WHERE status = 'open' GROUP BY kind").fetchall()
    if not rows:
        return None
    path = Path(alert_log or ALERT_LOG)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (f"{clinic_time.stamp()} | xray demo incident | open: "
            + ", ".join(f"{k} {n}" for k, n in rows) + "\n")
    with open(path, "a") as f:
        f.write(line)
    return line


def acknowledge(conn, incident_id, actor, role):
    _require(conn, actor, role, "manage_xray_demo", "xray_demo_acknowledge", f"demo-incident:{incident_id}")
    conn.execute("UPDATE xray_demo_incidents SET status = 'acknowledged', acknowledged_by = ?, acknowledged_at = ?"
                 " WHERE id = ? AND status = 'open'", (actor, clinic_time.stamp(), incident_id))
    conn.commit()
    _audit(conn, actor, role, "xray_demo_acknowledge", f"demo-incident:{incident_id}", 1)


def incidents(conn, status=None):
    sql, args = "SELECT * FROM xray_demo_incidents", ()
    if status:
        sql, args = sql + " WHERE status = ?", (status,)
    return [dict(r) for r in conn.execute(sql + " ORDER BY id", args)]


def released(conn):
    """the only way demo output leaves the queue: reviewed by a dentist, accepted or corrected."""
    out = []
    for r in conn.execute("SELECT j.id, j.input_sha256, j.detector, j.result, v.decision, v.corrected, v.reviewer"
                          " FROM xray_demo_jobs j JOIN xray_demo_reviews v ON v.id = j.review_id"
                          " WHERE v.decision IN ('accept', 'correct') ORDER BY j.id"):
        marks = json.loads(r["corrected"]) if r["decision"] == "correct" else json.loads(r["result"])
        out.append({"job_id": r["id"], "input_sha256": r["input_sha256"], "detector": r["detector"],
                    "decision": r["decision"], "reviewer": r["reviewer"], "marks": marks, "label": LABEL})
    return out


# --- reproduction and drift --------------------------------------------------------------


def reproduce(conn, jid):
    """re-run the stored image with the stored version; True when the stored result comes back."""
    j = job(conn, jid)
    if j is None or j["detector"] not in DETECTORS:
        return False
    pixels = xd.read(j["image"])["pixels"]
    if j["status"] == "abstained":
        return xd._std(pixels) < xd.FLAT_STD
    return DETECTORS[j["detector"]](pixels) == json.loads(j["result"])


def baseline(bench, env=None):
    runs = [xd.analyse(c["image"], env=env) for c in bench["cases"]]
    return {"abstention": sum(r["status"] == "abstained" for r in runs) / len(runs),
            "marks_per_image": sum(len(r["marks"]) for r in runs) / len(runs)}


def drift(conn, base, window=DRIFT_WINDOW):
    rows = conn.execute("SELECT status, result FROM xray_demo_jobs WHERE status IN ('result', 'abstained')"
                        " ORDER BY id DESC LIMIT ?", (window,)).fetchall()
    if not rows:
        return {"window": 0, "alert": None}
    abst = sum(r["status"] == "abstained" for r in rows) / len(rows)
    marks = sum(len(json.loads(r["result"])) for r in rows) / len(rows)
    alert = None
    if abs(abst - base["abstention"]) > DRIFT_ABSTENTION:
        alert = f"abstention rate {abst:.2f} against a baseline of {base['abstention']:.2f}"
    elif abs(marks - base["marks_per_image"]) > DRIFT_MARKS:
        alert = f"marks per image {marks:.2f} against a baseline of {base['marks_per_image']:.2f}"
    return {"window": len(rows), "abstention": round(abst, 3), "marks_per_image": round(marks, 3), "alert": alert}


# --- report ---------------------------------------------------------------------------------


def report(conn, base=None):
    by_status = dict(conn.execute("SELECT status, COUNT(*) FROM xray_demo_jobs GROUP BY status").fetchall())
    reviewed = conn.execute("SELECT COUNT(*) FROM xray_demo_jobs WHERE review_id IS NOT NULL").fetchone()[0]
    decisions = dict(conn.execute("SELECT v.decision, COUNT(*) FROM xray_demo_jobs j JOIN xray_demo_reviews v"
                                  " ON v.id = j.review_id GROUP BY v.decision").fetchall())
    finished = by_status.get("result", 0) + by_status.get("abstained", 0)
    return {
        "label": LABEL,
        "jobs": by_status, "reviewed": reviewed, "released": len(released(conn)),
        "review_coverage": round(reviewed / finished, 3) if finished else None,
        "decisions": decisions,
        "incidents": {k: n for k, n in conn.execute("SELECT kind, COUNT(*) FROM xray_demo_incidents GROUP BY kind")},
        "incidents_open": len(incidents(conn, status="open")),
        "versions_used": sorted({r[0] for r in conn.execute("SELECT DISTINCT detector FROM xray_demo_jobs"
                                                              " WHERE detector IS NOT NULL")}),
        "validated_versions": sorted({r[0] for r in conn.execute("SELECT version FROM xray_demo_validations"
                                                                   " WHERE passed = 1")}),
        "drift": drift(conn, base) if base else "not measured (no baseline given)",
        "decision": {"continue_fix_stop": None, "decided_by": None},
    }


def selftest():
    import xray_pilot_demo_selftest
    xray_pilot_demo_selftest.selftest()


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        selftest()
    else:
        print("see the module docstring; run xray_pilot_demo_selftest.py --selftest")
