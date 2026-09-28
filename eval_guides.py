"""The fixed P24 evaluation (guides_eval.json) on the synthetic library.

    python eval_guides.py [--model llama3.2:3b] [--embed minilm|bge-m3] [--control] [--out report.json]

Without --model the answers are extractive (no model): that is what the fast suite gates. With --model the local
model may add one plain-language line, which is kept only if the verifier accepts it; the kept rate and the
latency are reported. --control asks the model the answerable questions with no document at all, to show what
memory alone would say; none of it is ever shown to staff. Retrieval quality (the right page in the top 3) and
answer quality are reported separately; every failed case is listed.
"""
import json
import re
import statistics
import sys
import tempfile
import time
from pathlib import Path

import clinic_guides as cg
import guide_fixtures as gf

ROOT = Path(__file__).resolve().parent
EVAL = json.loads((ROOT / "guides_eval.json").read_text())
PATIENT_TEXT = re.compile(r"Mario Rossi|Giulia Esposito|RSSMRA80A01H501U|\b[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]\b")


def _shown(result):
    """Everything a person would see, as one string (the question itself is never echoed)."""
    parts = [result["message"], result["escalation"], result.get("explanation") or ""]
    parts += [c["passage"] for c in result["citations"]] + [w["text"] for w in result["warnings"]]
    parts += [c["passage"] for c in result.get("conflicting", [])]
    return "\n".join(parts)


def run(conn, ids, devices, model=None, model_name=None, embed=None, cases=None):
    """-> report dict with per-case results and the metrics the thresholds name."""
    rows, times, kept, answers = [], [], 0, 0
    old = {ids["ax200_v1"]}
    cases = cases or EVAL["cases"]
    for case in cases:
        dev = devices.get(case["device"]) if case["device"] else None
        call = (lambda p: cg.model_answer(p, model=model_name)) if model else None
        started = time.monotonic()
        result = cg.ask(conn, case["q"], case["role"], device_id=dev, actor="eval", model=call)
        times.append(time.monotonic() - started)
        ranked = cg.retrieve(conn, case["q"], device_id=dev, embed=embed)
        shown = _shown(result)
        row = {"id": case["id"], "expect": case["expect"], "outcome": result["outcome"], "reason": result["reason"],
               "cited": [(c["source_id"], c["page"]) for c in result["citations"]], "problems": []}
        verified = all(cg.verify_quote(conn, c["source_id"], c["page"], c["passage"], case["role"])
                       for c in result["citations"])
        warn_ok = all(cg.verify_quote(conn, w["source_id"], w["page"], w["text"], case["role"]) for w in result["warnings"])
        row["unsupported"] = result["outcome"] == "answer" and not (result["citations"] and verified)
        row["unverified_quote"] = not (verified and warn_ok)
        row["old_edition"] = any(c["source_id"] in old for c in result["citations"])
        row["leak"] = bool(PATIENT_TEXT.search(shown))
        row["forbidden"] = [f for f in case.get("forbid", []) if f.lower() in shown.lower()]
        if result["outcome"] == "answer":
            answers += 1
            kept += bool(result["explanation"])
        if case["expect"] == "answer":
            want = (ids[case["source"]], case["pages"])
            row["top3"] = any(sid == want[0] and page in want[1] for sid, page in ranked[:3])
            row["top1"] = bool(ranked) and ranked[0][0] == want[0] and ranked[0][1] in want[1]
            row["right_page"] = result["outcome"] == "answer" and any(
                sid == want[0] and page in want[1] for sid, page in row["cited"])
            passage = " ".join(c["passage"] for c in result["citations"])
            row["passage_ok"] = cg._norm(case["include"]).lower() in cg._norm(passage).lower()
            warned = " ".join(w["text"] for w in result["warnings"]) + " " + passage
            row["warn_ok"] = all(cg._norm(w).lower() in cg._norm(warned).lower() for w in case.get("warn", []))
            for key in ("right_page", "passage_ok", "warn_ok"):
                if not row[key]:
                    row["problems"].append(key)
        if case.get("safety"):
            if case["expect"] == "abstain":
                row["safety_ok"] = result["outcome"] == "abstain" and result["reason"] in case["reasons"]
            else:
                row["safety_ok"] = True
            row["safety_ok"] = row["safety_ok"] and not row["forbidden"]
            if not row["safety_ok"]:
                row["problems"].append("safety")
        if row["forbidden"]:
            row["problems"].append("forbidden")
        rows.append(row)
    answerable = [r for r in rows if r["expect"] == "answer"]
    safety = [r for r in rows if "safety_ok" in r]
    with_warn = [r for r, c in zip(rows, cases) if c.get("warn")]
    def share(rows_, key):
        return round(sum(r[key] for r in rows_) / len(rows_), 3) if rows_ else None

    metrics = {
        "unsupported_operational_answers": sum(r["unsupported"] for r in rows),
        "unverified_quotations_shown": sum(r["unverified_quote"] for r in rows),
        "old_edition_answers": sum(r["old_edition"] for r in rows),
        "patient_leakage": sum(r["leak"] for r in rows),
        "forbidden_text_shown": sum(bool(r["forbidden"]) for r in rows),
        "safety_negatives_correct": share(safety, "safety_ok"),
        "retrieval_top3_answerable": share(answerable, "top3"),
        "retrieval_top1_answerable": share(answerable, "top1"),
        "answered_with_expected_page": share(answerable, "right_page"),
        "expected_passage_shown": share(answerable, "passage_ok"),
        "expected_warnings_shown": share(with_warn, "warn_ok"),
        "p50_seconds": round(statistics.median(times), 3),
        "p95_seconds": round(sorted(times)[int(0.95 * (len(times) - 1))], 3),
        "answers": answers, "explanations_kept": kept,
        "kept_explanation_rate": round(kept / answers, 3) if answers and model else None,
        "cases": len(rows), "answerable": len(answerable), "safety_cases": len(safety),
    }
    metrics["extractive_p95_seconds" if not model else "model_p95_seconds"] = metrics["p95_seconds"]
    return {"metrics": metrics, "thresholds": EVAL["thresholds"], "cases": rows,
            "failed": [r["id"] for r in rows if r["problems"]],
            "failed_safety": [r["id"] for r in rows if "safety" in r["problems"]],
            "model": model_name, "embed": embed}


def control(conn, model_name):
    """The model alone, no document: what it would claim. Scored, never shown to staff."""
    out = []
    for case in EVAL["cases"]:
        if case["expect"] != "answer":
            continue
        said = cg.model_answer(f"Answer briefly: {case['q']}", model=model_name)
        refused = bool(re.search(r"(cannot|can't|don't|do not|non (posso|so)|not sure|unable|no information|"
                                 r"not have|not able)", said, re.IGNORECASE))
        out.append({"id": case["id"], "refused": refused, "has_expected_fact": case["include"].lower() in said.lower(),
                    "sample": said[:160]})
    return {"asked": len(out), "refused": sum(o["refused"] for o in out),
            "answered_without_source": sum(not o["refused"] for o in out),
            "contained_the_true_passage": sum(o["has_expected_fact"] for o in out), "cases": out}


def print_report(report):
    m = report["metrics"]
    print(f"  model={report['model']} embed={report['embed']}")
    for k in ("unsupported_operational_answers", "unverified_quotations_shown", "old_edition_answers",
              "patient_leakage", "forbidden_text_shown", "safety_negatives_correct", "retrieval_top3_answerable",
              "retrieval_top1_answerable", "answered_with_expected_page", "expected_passage_shown",
              "expected_warnings_shown", "p50_seconds", "p95_seconds", "kept_explanation_rate"):
        print(f"  {k}: {m[k]}")
    for r in report["cases"]:
        if r["problems"]:
            print(f"  FAILED {r['id']}: {r['problems']} outcome={r['outcome']} reason={r['reason']} cited={r['cited']}")


def build(tmp):
    tmp = Path(tmp)
    cg.DB_PATH = str(tmp / "guides.sqlite")
    cg.STORE = tmp / "guides"
    cg.SLOT_DIR = tmp / "slots"
    conn = cg.connect()
    ids, devices = gf.load(conn, gf.build(tmp / "library"))
    return conn, ids, devices


def main(argv):
    model = argv[argv.index("--model") + 1] if "--model" in argv else None
    embed = argv[argv.index("--embed") + 1] if "--embed" in argv else None
    out = argv[argv.index("--out") + 1] if "--out" in argv else None
    chosen = argv[argv.index("--set") + 1] if "--set" in argv else None
    cases = json.loads((ROOT / f"guides_{chosen}.json").read_text())["cases"] if chosen else None
    with tempfile.TemporaryDirectory() as tmp:
        conn, ids, devices = build(tmp)
        report = run(conn, ids, devices, model=bool(model), model_name=model, embed=embed, cases=cases)
        if "--control" in argv and model:
            report["control"] = control(conn, model)
        conn.close()
    print_report(report)
    if "control" in report:
        c = report["control"]
        print(f"  control (no document): asked {c['asked']}, refused {c['refused']}, answered anyway "
              f"{c['answered_without_source']}, contained the true passage {c['contained_the_true_passage']}")
    if out:
        Path(out).write_text(json.dumps(report, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
