"""the X-ray gate (P18). there is no X-ray analysis in this code base; this module
is what keeps it that way until the people who own the decision say otherwise.

    .venv/bin/python xray_gate.py check                  # validate docs/xray/gate.json, print the decision
    .venv/bin/python xray_gate.py split-check FILE.csv   # patient leakage / duplicate images across splits
    .venv/bin/python xray_gate.py --selftest

- the checklist (docs/xray/gate.json) is validated: an item is PASS only with an
  owner, a version, a date and evidence, and only after every item it requires
  is PASS - an evaluation cannot pass before its criteria do (P18.T1, P18.T3).
  GO needs every item PASS and a named decider and date. the agent cannot sign:
  this code only reads what people recorded.
- enabled() is the only way any later X-ray feature may ask whether it can run.
  it is False unless the gate is valid, the decision is GO, and the operator
  also set CLINIC_XRAY_ENABLED=1. today the decision is BLOCKED (D04).
- route_scan() fails when any of the three apps exposes an X-ray, radiograph,
  diagnosis or inference endpoint (P18.T4).
- split_check() audits a dataset manifest (image_sha256, patient_id, split):
  the same patient or the same image in two splits is leakage (P18.T2). it
  reads a manifest only; it never opens an image.
"""

import csv
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GATE = ROOT / "docs" / "xray" / "gate.json"
ENV = "CLINIC_XRAY_ENABLED"
DECISIONS = ("GO", "NO_GO", "BLOCKED")
STATUSES = ("PASS", "FAIL", "BLOCKED", "HUMAN_PENDING", "TBC")
FORBIDDEN_ROUTE = re.compile(r"x-?ray|radiograph|radiograf|diagnos|inference|finding|opg|cbct", re.I)


def _filled(value):
    return isinstance(value, str) and value.strip() not in ("", "TBC", "tbc", "unknown")


def problems(gate):
    """-> [str]; empty when the record is internally consistent (which is not the same as GO)."""
    out = []
    if gate.get("decision") not in DECISIONS:
        out.append(f"decision must be one of {DECISIONS}")
    items = {i.get("id"): i for i in gate.get("items", [])}
    for iid, item in items.items():
        status = item.get("status")
        if status not in STATUSES:
            out.append(f"{iid}: status {status!r} is not one of {STATUSES}")
            continue
        if status != "PASS":
            continue
        for field in ("owner", "version", "date", "evidence"):
            if not _filled(item.get(field)):
                out.append(f"{iid}: PASS without {field}")
        if _filled(item.get("date")):
            try:
                date.fromisoformat(item["date"])
            except ValueError:
                out.append(f"{iid}: date is not YYYY-MM-DD")
        for need in item.get("requires", []):
            if items.get(need, {}).get("status") != "PASS":
                out.append(f"{iid}: PASS before {need} is PASS")
    if gate.get("decision") == "GO":
        open_items = [iid for iid, i in items.items() if i.get("status") != "PASS"]
        if open_items:
            out.append(f"GO with items not PASS: {', '.join(open_items)}")
        if not (_filled(gate.get("decided_by")) and _filled(gate.get("decided_on"))):
            out.append("GO without a named decider and a date")
        if any(not _filled(v) for v in gate.get("intended_use", {}).values()):
            out.append("GO with the intended use not filled in")
    return out


def load(path=GATE):
    return json.loads(Path(path).read_text())


def enabled(gate=None, env=None):
    env = os.environ if env is None else env
    try:
        gate = load() if gate is None else gate
    except (OSError, ValueError):
        return False
    return (not problems(gate) and gate.get("decision") == "GO"
            and (env.get(ENV) or "").strip() == "1")


def route_scan(url_maps):
    """url_maps: [(app name, [(rule, endpoint)])] -> [str] offending routes."""
    return [f"{name}: {rule} ({endpoint})" for name, rows in url_maps for rule, endpoint in rows
            if FORBIDDEN_ROUTE.search(rule) or FORBIDDEN_ROUTE.search(endpoint)]


def app_routes():
    import release_docs
    return [(name, [(r, e) for r, _m, e in rows]) for name, rows in release_docs.routes()]


def split_check(rows):
    """rows: dicts with image_sha256, patient_id, split -> {problem: [...]}"""
    patient_splits, image_splits, missing = {}, {}, []
    for n, row in enumerate(rows, 1):
        pid, sha, split = (row.get(k, "").strip() for k in ("patient_id", "image_sha256", "split"))
        if not (pid and sha and split):
            missing.append(n)
            continue
        patient_splits.setdefault(pid, set()).add(split)
        image_splits.setdefault(sha, []).append(split)
    return {
        "patients_in_several_splits": sorted(p for p, s in patient_splits.items() if len(s) > 1),
        "images_in_several_splits": sorted(h for h, s in image_splits.items() if len(set(s)) > 1),
        "duplicate_images": sorted(h for h, s in image_splits.items() if len(s) > 1),
        "rows_missing_fields": missing,
    }


def main(argv):
    if "--selftest" in argv:
        selftest()
        return 0
    if argv[:1] == ["check"]:
        gate = load()
        bad = problems(gate) + route_scan(app_routes())
        print(json.dumps({"decision": gate["decision"], "reason": gate.get("reason"),
                          "enabled": enabled(gate), "problems": bad}, indent=1))
        return 1 if bad else 0
    if argv[:1] == ["split-check"] and len(argv) == 2:
        with open(argv[1], newline="") as f:
            found = split_check(list(csv.DictReader(f)))
        print(json.dumps(found, indent=1))
        return 1 if any(found.values()) else 0
    print(__doc__.split("\n\n")[1])
    return 2


def selftest():
    def item(iid, status="PASS", requires=None, **kw):
        base = {"id": iid, "what": "x", "status": status, "owner": "Dr Test", "version": "1",
                "date": "2026-09-24", "evidence": "doc.pdf"}
        if requires:
            base["requires"] = requires
        base.update(kw)
        return base

    filled = {k: "set" for k in ("country", "modality", "users", "output", "claims",
                                 "clinical_owner", "responsible_consultant")}
    go = {"decision": "GO", "decided_by": "Owner", "decided_on": "2026-09-24", "intended_use": filled,
          "items": [item("A"), item("CRIT"), item("EVAL", requires=["CRIT"])]}
    on = {ENV: "1"}

    # 1. the real record: consistent, BLOCKED, closed whatever the environment says
    real = load()
    assert problems(real) == [], problems(real)
    assert real["decision"] == "BLOCKED" and not enabled(real, on), "1: the gate must be closed today"
    assert all(i["status"] != "PASS" for i in real["items"]), "1: nothing is PASS today"

    # 2. a complete GO opens only with the operator switch as well
    assert problems(go) == [] and enabled(go, on) and not enabled(go, {}), "2: GO needs the switch too"

    # 3. unknown is never PASS
    for field, value in (("owner", None), ("owner", "TBC"), ("date", ""), ("evidence", "unknown"),
                         ("version", None)):
        bad = json.loads(json.dumps(go))
        bad["items"][0][field] = value
        assert f"A: PASS without {field}" in problems(bad) and not enabled(bad, on), (field, value)
    bad = json.loads(json.dumps(go))
    bad["items"][0]["date"] = "24/09/2026"
    assert "A: date is not YYYY-MM-DD" in problems(bad)

    # 4. an evaluation cannot pass before its criteria
    early = json.loads(json.dumps(go))
    early["items"][1]["status"] = "HUMAN_PENDING"
    assert "EVAL: PASS before CRIT is PASS" in problems(early) and not enabled(early, on)

    # 5. GO with anything open, unsigned, or without the intended use is refused
    for change in ({"decided_by": None}, {"decided_on": "TBC"}):
        bad = {**go, **change}
        assert "GO without a named decider and a date" in problems(bad) and not enabled(bad, on)
    bad = json.loads(json.dumps(go))
    bad["items"][0]["status"] = "BLOCKED"
    assert any(p.startswith("GO with items not PASS") for p in problems(bad)) and not enabled(bad, on)
    bad = json.loads(json.dumps(go))
    bad["intended_use"]["claims"] = "TBC"
    assert "GO with the intended use not filled in" in problems(bad) and not enabled(bad, on)
    assert not enabled({**go, "decision": "NO_GO"}, on) and not enabled({**go, "decision": "maybe"}, on)

    # 6. no x-ray or diagnosis endpoint in any app, today; and the scan does catch one
    assert route_scan(app_routes()) == [], route_scan(app_routes())
    fake = [("staff", [("/patients/<cf>/xray/analyse", "xray.analyse"), ("/login", "auth.login")]),
            ("patient", [("/me/diagnosis", "portal.diagnosis")])]
    assert len(route_scan(fake)) == 2, route_scan(fake)

    # 7. leakage: the same patient or the same image in two splits
    rows = [{"image_sha256": "a1", "patient_id": "P1", "split": "train"},
            {"image_sha256": "a2", "patient_id": "P1", "split": "test"},
            {"image_sha256": "b1", "patient_id": "P2", "split": "train"},
            {"image_sha256": "b1", "patient_id": "P3", "split": "test"},
            {"image_sha256": "c1", "patient_id": "P4", "split": "train"},
            {"image_sha256": "c1", "patient_id": "P4", "split": "train"},
            {"image_sha256": "", "patient_id": "P5", "split": "test"}]
    found = split_check(rows)
    assert found["patients_in_several_splits"] == ["P1"], found
    assert found["images_in_several_splits"] == ["b1"], found
    assert found["duplicate_images"] == ["b1", "c1"], found
    assert found["rows_missing_fields"] == [7], found
    clean = split_check([{"image_sha256": "d1", "patient_id": "P9", "split": "train"},
                         {"image_sha256": "d2", "patient_id": "P8", "split": "test"}])
    assert not any(clean.values()), clean
    print("selftest ok")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
