"""P19 (non-clinical demo, D04 DEMO_GO): what the demo pipeline must and must not do.

written before xray_demo.py. synthetic images only, a temporary database, the
demo switch set only inside this test.
"""

import json
import os
import struct
import sys
import tempfile
import zlib
from pathlib import Path

import xray_demo as xd
import xray_gate
from storage import init_db

ON = {xray_gate.DEMO_ENV: "1"}


def foreign_png(width=128, height=128, value=120):
    """a valid grayscale PNG that did not come from the generator - stands for a real image"""
    raw = b"".join(b"\x00" + bytes([value]) * width for _ in range(height))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def refused(fn, *args, **kw):
    try:
        fn(*args, **kw)
    except xd.Refused as e:
        return e.reason
    raise AssertionError(f"{fn.__name__} did not refuse")


def selftest():
    a = xd.synth(seed=1, marks=[("demo_mark_a", 20, 20, 8, 8), ("demo_mark_b", 80, 60, 10, 10)])

    # 1. default-off: without the demo switch nothing runs, even on a valid synthetic image
    assert "switched off" in refused(xd.analyse, a, env={}), "1: the demo must be off by default"
    assert "switched off" in refused(xd.analyse, a, env={xray_gate.ENV: "1"}), "1: the clinical switch is not the demo switch"

    # 2. input validation: only the generator's images, never modified
    meta = xd.validate(a)
    assert meta["width"] == 128 and meta["height"] == 128 and meta["generator"] == xd.GENERATOR, meta
    before = bytes(a)
    xd.analyse(a, env=ON)
    assert a == before, "2: the input bytes changed"
    assert "not a synthetic demo image" in refused(xd.validate, foreign_png()), "2: a real-looking image was accepted"
    tampered = bytearray(a)
    idat = tampered.find(b"IDAT")
    tampered[idat + 12] ^= 0x01
    assert refused(xd.validate, bytes(tampered)), "2: a damaged image was accepted"
    # a well-formed PNG whose pixels were changed after generation, original tag kept
    img = xd.read(a)
    img["pixels"][5][5] = (img["pixels"][5][5] + 1) % 256
    assert "changed" in refused(xd.validate, xd.write(img["pixels"], img["tags"])), "2: a changed image was accepted"
    assert "not a PNG" in refused(xd.validate, b"GIF89a" + b"\x00" * 64)
    assert "too large" in refused(xd.validate, a + b"\x00" * (xd.MAX_BYTES + 1))
    assert "out of spec" in refused(xd.validate, xd.synth(seed=2, marks=[], size=(32, 32)))

    # 3. analysis: demo marks found, labelled demo, no confidence anywhere, versions recorded
    r = xd.analyse(a, env=ON)
    assert r["status"] == "result" and r["label"] == xd.DEMO_LABEL, r
    assert r["input_sha256"] == meta["sha256"] and r["detector"] == xd.DETECTOR, r
    kinds = sorted(m["kind"] for m in r["marks"])
    assert kinds == ["demo_mark_a", "demo_mark_b"], r["marks"]
    # the label is the disclaimer and must say what this is not; everything else must not use clinical words
    assert "not a diagnosis" in r["label"].lower() and "not for clinical use" in r["label"].lower()
    text = json.dumps({k: v for k, v in r.items() if k != "label"}).lower()
    for word in ("confidence", "probability", "diagnos", "caries", "lesion", "finding\"", "score"):
        assert word not in text, f"3: {word!r} in the demo output"

    # 4. overlay: the same image, the same scale, and it says what it is
    over = xd.overlay(a, r["marks"])
    om = xd.read(over)
    assert (om["width"], om["height"]) == (128, 128), "4: the overlay changed the scale"
    assert om["tags"].get(xd.TAG_KEY, "").startswith("overlay-of=" + meta["sha256"]), om["tags"]
    assert "not a synthetic demo image" in refused(xd.validate, over), "4: an overlay is not an input"

    # 5. abstention: flat image, a failing detector, a timeout - a reason, never a result
    flat = xd.synth(seed=3, marks=[], noise=0, background=128)
    f = xd.analyse(flat, env=ON)
    assert f["status"] == "abstained" and "flat" in f["reason"] and f["marks"] == [], f

    def broken(_img):
        raise RuntimeError("detector crashed")
    g = xd.analyse(a, env=ON, detector=broken)
    assert g["status"] == "abstained" and "detector failed" in g["reason"] and g["marks"] == [], g

    def slow(_img):
        import time
        time.sleep(0.3)
        return []
    h = xd.analyse(a, env=ON, detector=slow, timeout=0.05)
    assert h["status"] == "abstained" and "timed out" in h["reason"], h

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        conn = init_db(str(tmp / "demo.sqlite"))

        # 6. reviews: dentist only, append-only, audited, nothing written to a patient record
        rid = xd.record_review(conn, r, "accept", "dentist", "dentist")
        xd.record_review(conn, r, "correct", "dentist", "dentist",
                         corrected=[{"kind": "demo_mark_a", "x": 20, "y": 20, "w": 8, "h": 8}])
        assert "not allowed" in refused(xd.record_review, conn, r, "accept", "assistant", "assistant")
        assert "not allowed" in refused(xd.record_review, conn, r, "accept", "admin", "admin")
        assert "decision" in refused(xd.record_review, conn, r, "diagnose", "dentist", "dentist")
        assert "correction" in refused(xd.record_review, conn, r, "correct", "dentist", "dentist")
        rows = xd.reviews(conn)
        assert [x["decision"] for x in rows] == ["accept", "correct"], rows
        assert rows[0]["input_sha256"] == meta["sha256"] and rows[0]["detector"] == xd.DETECTOR
        assert json.loads(rows[0]["output"]) == r["marks"], "6: the reviewed output must be recoverable"
        for sql in ("UPDATE xray_demo_reviews SET decision = 'reject' WHERE id = ?",
                    "DELETE FROM xray_demo_reviews WHERE id = ?"):
            try:
                conn.execute(sql, (rid,))
                raise AssertionError(f"6: {sql.split()[0]} was allowed")
            except Exception as e:
                assert "append-only" in str(e), e
        audited = conn.execute("SELECT allowed FROM audit_log WHERE action = 'xray_demo_review' ORDER BY id").fetchall()
        assert [x[0] for x in audited] == [1, 1, 0, 0], audited
        assert conn.execute("SELECT COUNT(*) FROM visits").fetchone()[0] == 0, "6: a patient record was written"

        # 7. benchmark: patient-level split, locked criteria, metrics with intervals, no retroactive change
        bench = xd.benchmark(n=40, seed=7)
        assert not any(xray_gate.split_check(bench["manifest"]).values()), "7: leakage in the benchmark"
        criteria = tmp / "criteria.json"
        criteria.write_text(json.dumps(xd.DEFAULT_CRITERIA))
        lock = xd.lock(criteria, bench["manifest"])
        report = xd.evaluate(bench, criteria, lock, env=ON)
        for kind in ("demo_mark_a", "demo_mark_b"):
            m = report["per_kind"][kind]
            assert m["level"] == "image" and 0 <= m["sensitivity"]["value"] <= 1, m
            lo, hi = m["sensitivity"]["ci95"]
            assert lo <= m["sensitivity"]["value"] <= hi, m
            assert "calibration" not in m or m["calibration"] == "not applicable: no confidence output"
        assert report["abstained"] >= 0 and report["criteria_sha256"] == lock["criteria_sha256"]
        criteria.write_text(json.dumps({**xd.DEFAULT_CRITERIA, "min_sensitivity": 0.1}))
        assert "criteria changed" in refused(xd.evaluate, bench, criteria, lock, env=ON), "7: threshold moved after lock"
        leaky = {**bench, "manifest": bench["manifest"] + [{**bench["manifest"][0], "split": "train"
                                                           if bench["manifest"][0]["split"] == "test" else "test"}]}
        assert "leak" in refused(xd.lock, criteria, leaky["manifest"]), "7: a leaky manifest was locked"
        # 7b. an abstention is counted apart, never as a result
        import hashlib
        flat_sha = hashlib.sha256(flat).hexdigest()
        bench2 = {"cases": bench["cases"] + [{"image": flat, "truth": [], "noise": 0, "sha": flat_sha}],
                  "manifest": bench["manifest"] + [{"image_sha256": flat_sha, "patient_id": "SYN9999", "split": "test"}]}
        criteria.write_text(json.dumps(xd.DEFAULT_CRITERIA))
        r2 = xd.evaluate(bench2, criteria, xd.lock(criteria, bench2["manifest"]), env=ON)
        r1 = xd.evaluate(bench, criteria, xd.lock(criteria, bench["manifest"]), env=ON)
        assert r2["abstained"] == r1["abstained"] + 1, (r1["abstained"], r2["abstained"])
        for kind in ("demo_mark_a", "demo_mark_b"):
            assert r2["per_kind"][kind]["counts"] == r1["per_kind"][kind]["counts"], "7b: an abstention was scored"
        conn.close()

    # 8. metrics arithmetic on a fixed table (Wilson interval)
    s = xd.rate(8, 10)
    assert s["value"] == 0.8 and s["ci95"] == [0.4902, 0.9433], s
    assert xd.rate(0, 0) == {"value": None, "ci95": None, "n": 0}
    print("selftest ok")


def main():
    if "--selftest" not in sys.argv:
        print("usage: python xray_demo_selftest.py --selftest")
        return
    os.environ.pop(xray_gate.DEMO_ENV, None)
    selftest()


if __name__ == "__main__":
    main()
