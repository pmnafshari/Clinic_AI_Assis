"""P20 (demo pilot machinery on the P19 synthetic demo): what must hold before any real pilot.

written before xray_pilot_demo.py. synthetic images, a temporary database, the demo switch set only here.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import xray_demo as xd
import xray_gate
import xray_pilot_demo as xp
from storage import init_db

ON = {xray_gate.DEMO_ENV: "1"}


def refused(fn, *args, **kw):
    try:
        fn(*args, **kw)
    except xd.Refused as e:
        return e.reason
    raise AssertionError(f"{fn.__name__} did not refuse")


def images(n, seed=11):
    return [xd.synth(seed=seed + i, marks=[("demo_mark_a", 10 + i, 20, 8, 8)] if i % 2 else []) for i in range(n)]


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        conn = init_db(str(tmp / "pilot.sqlite"))
        alerts = tmp / "alerts.log"
        criteria = tmp / "criteria.json"
        criteria.write_text(json.dumps(xd.DEFAULT_CRITERIA))
        bench = xd.benchmark(n=20, seed=3)
        locked = xd.lock(criteria, bench["manifest"])

        # 1. switched off (the default): nothing is queued, nothing runs
        assert "switched off" in refused(xp.enqueue, conn, images(1)[0], "dentist", "dentist", env={})

        # 2. model change policy: no version runs before it passed revalidation on the locked benchmark
        imgs = images(4)
        ids = [xp.enqueue(conn, im, "dentist", "dentist", env=ON) for im in imgs]
        assert "not validated" in refused(xp.run_queue, conn, env=ON), "2: an unvalidated version ran"
        assert "not validated" in refused(xp.set_version, conn, "demo-marks-threshold-0", "dentist", "dentist"), \
            "2: rollback to a known but unvalidated version"
        for version in xp.DETECTORS:
            v = xp.revalidate(conn, version, bench, criteria, locked, "dentist", "dentist", env=ON)
            assert v["passed"], v
        assert "not validated" in refused(xp.set_version, conn, "demo-marks-threshold-9", "dentist", "dentist")

        # 3. the kill switch: anyone may stop; queued work is cancelled, nothing new is queued; a dentist resumes
        xp.kill(conn, "assistant", "assistant", "drill: stop everything")
        done = xp.run_queue(conn, env=ON)
        assert done == {"processed": 0, "cancelled": 4}, done
        assert {j["status"] for j in xp.jobs(conn)} == {"cancelled"}, "3: a queued job survived the kill"
        assert "stopped" in refused(xp.enqueue, conn, imgs[0], "dentist", "dentist", env=ON)
        assert "not allowed" in refused(xp.resume, conn, "assistant", "assistant")
        xp.resume(conn, "dentist", "dentist")
        ids = [xp.enqueue(conn, im, "dentist", "dentist", env=ON) for im in imgs]
        assert xp.run_queue(conn, env=ON) == {"processed": 4, "cancelled": 0}

        # 4. mandatory review: nothing is released until a dentist reviews it; a reject is never released
        assert xp.released(conn) == [], "4: output released without review"
        assert "not allowed" in refused(xp.review, conn, ids[0], "accept", "assistant", "assistant")
        xp.review(conn, ids[0], "accept", "dentist", "dentist")
        xp.review(conn, ids[1], "reject", "dentist", "dentist", incident="false_positive")
        xp.review(conn, ids[2], "correct", "dentist", "dentist",
                  corrected=[{"kind": "demo_mark_a", "x": 1, "y": 1, "w": 8, "h": 8}], incident="false_negative",
                  alert_log=alerts)
        out = {r["job_id"]: r for r in xp.released(conn)}
        assert set(out) == {ids[0], ids[2]}, f"4: released {sorted(out)}"
        assert out[ids[2]]["marks"] == [{"kind": "demo_mark_a", "x": 1, "y": 1, "w": 8, "h": 8}], "4: the correction was lost"
        assert "already reviewed" in refused(xp.review, conn, ids[0], "reject", "dentist", "dentist")

        # 5. incidents escalate until someone acknowledges them; the alert line carries no image or hash
        open_ = xp.incidents(conn, status="open")
        assert sorted(i["kind"] for i in open_) == ["false_negative", "false_positive"], open_
        assert alerts.exists(), "5: the incident was not escalated"
        line = alerts.read_text()
        assert "xray demo incident" in line and "false_negative" in line, line
        assert all(j["input_sha256"][:12] not in line for j in xp.jobs(conn)), "5: a hash in the alert"
        assert "not allowed" in refused(xp.acknowledge, conn, open_[0]["id"], "assistant", "assistant")
        for inc in open_:
            xp.acknowledge(conn, inc["id"], "dentist", "dentist")
        assert xp.incidents(conn, status="open") == []

        # 6. historical reconstruction and rollback
        for j in xp.jobs(conn):
            if j["status"] in ("result", "abstained"):
                assert xp.reproduce(conn, j["id"]), f"6: job {j['id']} does not reproduce"
        # a stored result that was altered afterwards must not "reproduce"
        tampered = xp.enqueue(conn, imgs[1], "dentist", "dentist", env=ON)
        xp.run_queue(conn, env=ON)
        conn.execute("UPDATE xray_demo_jobs SET result = '[]' WHERE id = ?", (tampered,))
        conn.commit()
        assert not xp.reproduce(conn, tampered), "6: an altered result reproduced"
        xp.set_version(conn, "demo-marks-threshold-0", "dentist", "dentist")
        new = xp.enqueue(conn, imgs[3], "dentist", "dentist", env=ON)
        xp.run_queue(conn, env=ON)
        assert xp.job(conn, new)["detector"] == "demo-marks-threshold-0", "6: rollback not applied"
        assert xp.reproduce(conn, ids[0]), "6: an older job no longer reproduces after the rollback"
        xp.set_version(conn, xd.DETECTOR, "dentist", "dentist")

        # 7. drift: the recent window against the benchmark baseline
        base = xp.baseline(bench, env=ON)
        assert xp.drift(conn, base)["alert"] is None, xp.drift(conn, base)
        flat = xd.synth(seed=99, marks=[], noise=0, background=128)
        for _ in range(8):
            xp.enqueue(conn, flat, "dentist", "dentist", env=ON)
        xp.run_queue(conn, env=ON)
        d = xp.drift(conn, base)
        assert d["alert"] and "abstention" in d["alert"], d

        # 8. data protection: the audit trail names hashes and ids, never image bytes
        audit = " ".join(str(tuple(r)) for r in conn.execute("SELECT * FROM audit_log"))
        assert "PNG" not in audit and "IHDR" not in audit, "8: image data in the audit trail"
        assert conn.execute("SELECT COUNT(*) FROM visits").fetchone()[0] == 0, "8: a patient record was written"

        # 9. the report states facts and leaves the decision to people
        rep = xp.report(conn)
        assert rep["decision"] == {"continue_fix_stop": None, "decided_by": None}, rep["decision"]
        assert rep["reviewed"] == 3 and rep["released"] == 2 and rep["incidents"]["false_negative"] == 1, rep
        assert "not a clinical pilot" in rep["label"].lower()

        # 10. a broken gate record turns the demo off, and the apps still start
        broken = tmp / "gate.json"
        broken.write_text(json.dumps({**xray_gate.load(), "decided_by": None}))
        gate = json.loads(broken.read_text())
        assert xray_gate.problems(gate) and not xray_gate.demo_enabled(gate, ON), "10: a broken gate opened"
        assert "switched off" in refused(xp.enqueue, conn, imgs[0], "dentist", "dentist", env=ON, gate=gate)
        from app import create_app
        assert create_app().test_client().get("/login").status_code == 200, "10: the staff app broke"
        conn.close()
    print("selftest ok")


def main():
    if "--selftest" not in sys.argv:
        print("usage: python xray_pilot_demo_selftest.py --selftest")
        return
    os.environ.pop(xray_gate.DEMO_ENV, None)
    selftest()


if __name__ == "__main__":
    main()
