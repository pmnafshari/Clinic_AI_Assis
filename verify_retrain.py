"""Post-retrain verification: did the new weights actually fix what they were for?

Run this after `ollama create dental-notes -f Modelfile` with freshly trained
weights. It answers three questions in one pass, so the answer does not depend
on remembering which numbers mattered:

  1. did any gated field REGRESS against the recorded baseline
  2. do the three terms that failed in later position now translate
  3. should e2e_intake_walk check 5c be flipped from pinning the defect to
     asserting the fix

**A rising aggregate is not a pass.** The aggregate hides a field going
backwards behind two others going forwards, and the two defects this retrain
targets sit in the two weakest fields. So every field is compared on its own and
any drop fails the run, whatever the aggregate does.

usage:  .venv/bin/python verify_retrain.py [--baseline]

        --baseline  rewrite BASELINE.json from the current model instead of
                    checking against it. only after a run you have accepted.

needs:  ollama serve, and dental-notes registered from the new weights.
"""

import json
import sys
from pathlib import Path

import extract_note
from eval_notes import GATE_FIELDS, field_match

BASELINE_PATH = Path(__file__).resolve().parent / "retrain_baseline.json"
TEST_FILE = "notes_test.jsonl"

# the three surfaces measured failing in second position on 2026-09-05, and the
# code each has to become. first position already worked - that is why the note
# puts them second.
LATER_POSITION = [("igiene", "prophy"), ("pulizia", "prophy"), ("panoramica", "opg")]
PROBE_CF = "RSSM800010150100"

RESULTS = []


def check(step, ok, note):
    RESULTS.append((step, bool(ok), note))
    print(f"  {'PASS' if ok else 'FAIL'}  {step}: {note}")
    return bool(ok)


def score():
    """-> {field: pass rate} over the held-out test set."""
    rows = [json.loads(l) for l in open(TEST_FILE) if l.strip()]
    hits = {f: 0 for f in GATE_FIELDS}
    counted = 0
    for row in rows:
        try:
            note = extract_note.parse_reply(extract_note.call_model(row["input"]))
        except Exception as e:
            print(f"    extraction failed on one note: {type(e).__name__}")
            continue
        counted += 1
        pred = json.loads(note.model_dump_json())
        for f in GATE_FIELDS:
            if field_match(f, pred.get(f), row["output"].get(f)):
                hits[f] += 1
    if not counted:
        raise SystemExit("no notes could be scored - is ollama running?")
    out = {f: round(hits[f] / counted, 4) for f in GATE_FIELDS}
    out["aggregate"] = round(sum(out.values()) / len(GATE_FIELDS), 4)
    out["_notes"] = counted
    return out


def later_position():
    """Does each Italian term translate when it is NOT the first procedure?"""
    out = {}
    for term, code in LATER_POSITION:
        note = f"{PROBE_CF} Mario Rossi, comp 22, {term} 11, fu 2wk"
        try:
            got = extract_note.parse_reply(extract_note.call_model(note)).procedures
        except Exception as e:
            out[term] = ("error", f"{type(e).__name__}")
            continue
        low = [p.lower() for p in got]
        if any(p.startswith(code) for p in low):
            out[term] = ("fixed", got)
        elif any(p.startswith(term) for p in low):
            out[term] = ("still raw", got)
        else:
            out[term] = ("unsafe", got)
    return out


def main():
    if not BASELINE_PATH.exists():
        raise SystemExit(f"no {BASELINE_PATH.name} - commit one before comparing")
    baseline = json.loads(BASELINE_PATH.read_text())

    print("scoring the registered dental-notes over", TEST_FILE)
    now = score()
    print(f"  scored {now['_notes']} notes\n")

    if "--baseline" in sys.argv:
        keep = {k: v for k, v in now.items() if not k.startswith("_")}
        BASELINE_PATH.write_text(json.dumps(keep, indent=2) + "\n")
        print(f"baseline rewritten: {keep}")
        return 0

    print("field                  baseline    now      delta")
    regressed = []
    for f in GATE_FIELDS + ["aggregate"]:
        was, is_ = baseline.get(f), now[f]
        if was is None:
            continue
        delta = round(is_ - was, 4)
        flag = ""
        if delta < 0:
            flag = "   REGRESSION"
            regressed.append((f, was, is_))
        elif delta > 0:
            flag = "   improved"
        print(f"  {f:<20} {was:<11} {is_:<8} {delta:+}{flag}")

    print()
    check("no gated field regressed", not regressed,
          "every field held or improved" if not regressed else
          f"{[f for f, _, _ in regressed]} went backwards - a higher aggregate does not "
          f"excuse this, fix or accept it deliberately")

    print("\nlater-position translation:")
    lp = later_position()
    for term, (state, got) in lp.items():
        code = dict(LATER_POSITION)[term]
        check(f"{term} -> {code} in second position", state == "fixed",
              f"{state}: {got}")

    fixed_igiene = lp.get("igiene", ("", ""))[0] == "fixed"
    print()
    if fixed_igiene:
        print("  igiene now normalises. e2e_intake_walk check 5c pins the DEFECT and")
        print("  will fail - that is the success signal, not a regression. flip it to")
        print("  assert prophy and delete the pin.")
    else:
        print("  igiene still raw in second position: check 5c should stay as a pin.")

    failed = [s for s, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("failed:", ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
