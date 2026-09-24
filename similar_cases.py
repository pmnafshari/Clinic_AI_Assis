"""Similar past cases (P16). Past records as evidence, never a recommendation.

WHAT A CASE IS. A visit of ANOTHER patient that carries a review record
(visit_reviews). An unreviewed visit is never shown as evidence to anyone.

HOW CASES ARE MATCHED. CRITERIA below - a draft. No clinical owner has set
them (P16.01 BLOCKED, POL-17) and every page says so. A case must share a
recorded procedure code with the source visit; the same FDI tooth, the same
quadrant and shared note words add to its score. Codes are compared as the
strings that were recorded: the shorthand glossary is not approved (POL-11), so
nothing here says what a code means. Every result lists why it matched.

WHAT IS SHOWN. Recorded procedures, teeth, the month, and - in the default
view - a short note excerpt with every patient name, codice fiscale, phone
number and e-mail address redacted. The teaching view drops the note text.
No outcome is recorded anywhere in this schema, so every case says "no outcome
recorded"; nothing is ever called successful. Note text is data: it cannot
change the query, the criteria or a permission.

FEEDBACK. A dentist can mark a case similar / not similar, or remove it from
similar-case search. Both are append-only, carry the criteria version, are
audited, and change neither a visit nor the ranking.

Dentist only (read_clinical). Patients have no route here.

OFF BY DEFAULT. The criteria are an unapproved draft, so nothing here runs unless
the clinic starts the app with CLINIC_SIMILAR_CASES=1. Switched off, a dentist is
told so, nothing is read or written, and the refusal is audited.
"""
import json
import os
import re

import clinic_time
from auth import authorize, log_audit

CAPABILITY = "read_clinical"
ENV_FLAG = "CLINIC_SIMILAR_CASES"
OFF_MESSAGE = ("Similar cases is switched off on this system: its matching criteria are a draft"
               " that no clinical owner has approved.")
CRITERIA = {
    "version": "p16.1-draft",
    "approved_by": None,
    "require": "at least one recorded procedure code in common",
    "weights": {"code": 3, "tooth": 2, "quadrant": 1, "word": 0.5},
    "max_words": 4,
}
MIN_POOL = 3
POOL_LIMIT = 2000
TOP_K = 10
EXCERPT = 200
NO_OUTCOME = "no outcome recorded"
CRITERIA_NOTE = ("Matching criteria are a draft, not approved by a clinical owner. These are past"
                 " records for comparison, not advice about this patient.")
VERDICTS = ("similar", "not_similar")

SCHEMA = """
    CREATE TABLE IF NOT EXISTS similar_case_feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_visit_id INTEGER NOT NULL,
        case_visit_id INTEGER NOT NULL,
        verdict TEXT NOT NULL CHECK (verdict IN ('similar', 'not_similar')),
        reason TEXT,
        criteria_version TEXT NOT NULL,
        decided_by TEXT NOT NULL,
        decided_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_similar_feedback_source
        ON similar_case_feedback (source_visit_id, case_visit_id);
    CREATE TABLE IF NOT EXISTS similar_case_exclusions (
        visit_id INTEGER PRIMARY KEY,
        reason TEXT,
        criteria_version TEXT NOT NULL,
        decided_by TEXT NOT NULL,
        decided_at TEXT NOT NULL
    );
    CREATE TRIGGER IF NOT EXISTS similar_case_feedback_no_update
        BEFORE UPDATE ON similar_case_feedback
        BEGIN SELECT RAISE(ABORT, 'similar-case feedback is append-only'); END;
    CREATE TRIGGER IF NOT EXISTS similar_case_feedback_no_delete
        BEFORE DELETE ON similar_case_feedback
        WHEN NOT EXISTS (SELECT 1 FROM audit_unlock)
        BEGIN SELECT RAISE(ABORT, 'similar-case feedback is append-only'); END;
    CREATE TRIGGER IF NOT EXISTS similar_case_exclusions_no_update
        BEFORE UPDATE ON similar_case_exclusions
        BEGIN SELECT RAISE(ABORT, 'similar-case exclusions are append-only'); END;
    CREATE TRIGGER IF NOT EXISTS similar_case_exclusions_no_delete
        BEFORE DELETE ON similar_case_exclusions
        WHEN NOT EXISTS (SELECT 1 FROM audit_unlock)
        BEGIN SELECT RAISE(ABORT, 'similar-case exclusions are append-only'); END;
"""

# a tooth is a two-digit FDI number standing alone - not part of a date or a phone
TOOTH = re.compile(r"(?<![\d/.\-])([1-4][1-8])(?![\d/.\-])")
WORD = re.compile(r"[a-zà-ù]{4,}")
STOP = {"della", "delle", "dello", "degli", "nella", "nelle", "sulla", "sulle", "prima", "dopo",
        "with", "from", "this", "that", "patient", "paziente", "controllo", "visita", "seduta"}
# any 16-character token of letters and digits: a codice fiscale, real or synthetic
CF = re.compile(r"\b(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{16}\b")
PHONE = re.compile(r"\+?\d[\d .\-]{6,}\d")
EMAIL = re.compile(r"\S+@\S+")


class Disabled(RuntimeError):
    """The feature is switched off; nothing was read or written."""


def enabled():
    return os.environ.get(ENV_FLAG) == "1"


def _require(conn, actor, role, action, target):
    if not authorize(role, CAPABILITY):
        log_audit(conn, actor, role, action, target, allowed=0)
        raise PermissionError(f"{role} may not {action}")
    if not enabled():
        log_audit(conn, actor, role, action, target, allowed=0, reason="switched off")
        raise Disabled(OFF_MESSAGE)


def _procedures(raw):
    try:
        items = json.loads(raw or "[]")
    except ValueError:
        return []
    return [str(p).strip() for p in items if str(p).strip()] if isinstance(items, list) else []


def features(visit):
    procs = _procedures(visit["procedures"])
    codes = {p.split()[0].lower() for p in procs}
    teeth = set(TOOTH.findall(" ".join(procs) + " " + (visit["clinical_notes"] or "")))
    words = {w for w in WORD.findall((visit["clinical_notes"] or "").lower()) if w not in STOP}
    return {"procedures": procs, "codes": codes, "teeth": teeth,
            "quadrants": {t[0] for t in teeth}, "words": words}


def _score(src, case):
    """-> (score, reasons, strength), or None when the draft criteria say not similar."""
    shared_codes = src["codes"] & case["codes"]
    if not shared_codes:
        return None
    w = CRITERIA["weights"]
    teeth = src["teeth"] & case["teeth"]
    quadrants = (src["quadrants"] & case["quadrants"]) - {t[0] for t in teeth}
    words = sorted(src["words"] & case["words"])[:CRITERIA["max_words"]]
    reasons = [f"same recorded procedure code: {', '.join(sorted(shared_codes))}"]
    if teeth:
        reasons.append(f"same tooth: {', '.join(sorted(teeth))}")
    if quadrants:
        reasons.append(f"same quadrant: {', '.join(sorted(quadrants))}")
    if words:
        reasons.append(f"shared note words: {', '.join(words)}")
    score = (w["code"] * len(shared_codes) + w["tooth"] * len(teeth)
             + w["quadrant"] * len(quadrants) + w["word"] * len(words))
    if teeth:
        strength = "strong"
    elif quadrants or words:
        strength = "partial"
    else:
        strength = "weak"
    return score, reasons, strength


def _redactor(conn):
    names = set()
    for (name,) in conn.execute("SELECT patient_name FROM patients"):
        names |= {p for p in re.findall(r"\w+", name or "") if len(p) >= 3}
    by_name = re.compile(r"\b(" + "|".join(sorted(map(re.escape, names), key=len, reverse=True))
                         + r")\b", re.I) if names else None

    def redact(text):
        text = EMAIL.sub("[…]", text or "")
        text = CF.sub("[…]", text)
        text = PHONE.sub("[…]", text)
        return by_name.sub("[…]", text) if by_name else text
    return redact


def source(conn, visit_id, pid):
    v = conn.execute("SELECT * FROM visits WHERE id = ? AND patient_id = ?", (visit_id, pid)).fetchone()
    if v is None:
        raise LookupError("no such visit for this patient")
    return v


def find(conn, visit_id, pid, actor, role, view="minimised", k=TOP_K):
    """Cases like this visit, from other patients' reviewed visits. Never advice."""
    _require(conn, actor, role, "similar_cases", f"visit:{visit_id}")
    src_visit = source(conn, visit_id, pid)
    src = features(src_visit)
    pool = conn.execute(
        "SELECT v.* FROM visits v JOIN visit_reviews r ON r.visit_id = v.id"
        " WHERE v.patient_id != ?"
        " AND v.id NOT IN (SELECT visit_id FROM similar_case_exclusions)"
        " ORDER BY v.id DESC LIMIT ?", (pid, POOL_LIMIT)).fetchall()
    verdicts = {r["case_visit_id"]: r["verdict"] for r in conn.execute(
        "SELECT case_visit_id, verdict FROM similar_case_feedback WHERE source_visit_id = ?"
        " ORDER BY id", (visit_id,))}
    out = {"status": "ok", "results": [], "pool": len(pool), "view": view,
           "criteria": CRITERIA, "criteria_note": CRITERIA_NOTE,
           "source": {"procedures": src["procedures"], "teeth": sorted(src["teeth"])}}
    if not src["codes"]:
        out["status"] = "no_features"
        out["message"] = "This visit records no procedure code, so there is nothing to compare."
    elif len(pool) < MIN_POOL:
        out["status"] = "not_enough"
        out["message"] = (f"Only {len(pool)} reviewed case(s) from other patients - too few to"
                          " compare.")
    else:
        redact = _redactor(conn)
        scored = []
        for case in pool:
            hit = _score(src, features(case))
            if hit:
                scored.append((hit, case))
        scored.sort(key=lambda x: (-x[0][0], -x[1]["id"]))
        for (score, reasons, strength), case in scored[:k]:
            note = None
            if view != "teaching":
                note = redact(case["clinical_notes"])[:EXCERPT]
            out["results"].append({
                "visit_id": case["id"], "label": f"case {case['id']}",
                "month": (case["visit_date"] or "")[:7] or "date not recorded",
                "procedures": _procedures(case["procedures"]),
                "teeth": sorted(features(case)["teeth"]), "reasons": reasons,
                "strength": strength, "score": score, "outcome": NO_OUTCOME, "note": note,
                "your_verdict": verdicts.get(case["id"])})
        if out["results"]:
            out["message"] = (f"{len(out['results'])} of {len(pool)} reviewed cases from other"
                              " patients share a recorded procedure code.")
        else:
            out["status"] = "no_similar"
            out["message"] = (f"No similar case among {len(pool)} reviewed cases from other"
                              " patients.")
    log_audit(conn, actor, role, "similar_cases", f"visit:{visit_id}", allowed=1,
              reason=f"criteria {CRITERIA['version']}; view {view}; pool {len(pool)};"
                     f" results {len(out['results'])}")
    return out


def _case(conn, case_id, pid):
    """A reviewed visit of another patient, or LookupError."""
    c = conn.execute("SELECT v.* FROM visits v JOIN visit_reviews r ON r.visit_id = v.id"
                     " WHERE v.id = ? AND v.patient_id != ?", (case_id, pid)).fetchone()
    if c is None:
        raise LookupError("no such case")
    return c


def feedback(conn, visit_id, pid, case_id, verdict, reason, actor, role):
    _require(conn, actor, role, "similar_case_feedback", f"visit:{visit_id}")
    if verdict not in VERDICTS:
        raise ValueError("verdict must be similar or not_similar")
    source(conn, visit_id, pid)
    _case(conn, case_id, pid)
    conn.execute("INSERT INTO similar_case_feedback (source_visit_id, case_visit_id, verdict,"
                 " reason, criteria_version, decided_by, decided_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (visit_id, case_id, verdict, (reason or "")[:300], CRITERIA["version"], actor,
                  clinic_time.stamp()))
    conn.commit()
    log_audit(conn, actor, role, "similar_case_feedback", f"visit:{case_id}", allowed=1,
              reason=f"{verdict}; criteria {CRITERIA['version']}")


def exclude(conn, case_id, reason, actor, role, pid=""):
    """Remove a case from similar-case search for everyone. The visit itself is untouched."""
    _require(conn, actor, role, "similar_case_exclude", f"visit:{case_id}")
    _case(conn, case_id, pid)
    conn.execute("INSERT OR IGNORE INTO similar_case_exclusions (visit_id, reason, criteria_version,"
                 " decided_by, decided_at) VALUES (?, ?, ?, ?, ?)",
                 (case_id, (reason or "")[:300], CRITERIA["version"], actor,
                  clinic_time.stamp()))
    conn.commit()
    log_audit(conn, actor, role, "similar_case_exclude", f"visit:{case_id}", allowed=1,
              reason=f"criteria {CRITERIA['version']}")
