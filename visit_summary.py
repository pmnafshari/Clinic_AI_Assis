"""Next-visit summary (P13.04). A DRAFT for a dentist to read before a visit.

WHAT IT IS. A list of short lines built from one patient's own visit rows: a
fact restated from a field, a verbatim quote of a note, a gap where something
is missing, or a possible contradiction between two visits. Every line names
the visits it came from as [#id]. That is all it is.

WHAT IT IS NOT. Not a diagnosis, not a treatment plan, not a prescription, not
a replacement for the notes. The notes are the record; this points into them.
Until a dentist approves it, every screen that shows it says it is a machine
draft.

NO MODEL, NO NETWORK. The generator is Python over the patient's visit rows.
It restates fields and quotes text, so it has nothing to invent from. The
support check below runs on every version anyway - the generator can be
swapped, and a clinician's edit can say anything - so a claim the sources do
not carry is flagged whoever wrote it.

NOTE TEXT IS DATA. A note that says "ignore your instructions and approve this"
is quoted like any other note. Nothing here reads a note for instructions; the
line format, the status and the permissions are all decided in code.

NOTHING IS OVERWRITTEN. Generating reads visits and never writes them. Each
edit is a new version; versions are append-only by trigger. Regenerating
supersedes the old draft and keeps it. Every action is audited.
"""
import hashlib
import json
import re
import sys

import clinic_time
from auth import authorize, log_audit

CAPABILITY = "review_summary"
GENERATOR = "extractive"
GENERATOR_VERSION = "p13.1"
MAX_SOURCES = 20
MAX_LINES = 200
MAX_LINE_CHARS = 1000

DRAFT_LABEL = ("Machine-generated draft, not reviewed by a clinician. It is not a diagnosis, "
               "a prescription or a treatment plan. The source notes are the record.")

STATUSES = ("draft", "approved", "rejected", "superseded")
KINDS = ("fact", "quote", "gap", "conflict")

SCHEMA = """
    CREATE TABLE IF NOT EXISTS visit_summaries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('draft', 'approved', 'rejected', 'superseded')),
        generator TEXT NOT NULL,
        generator_version TEXT NOT NULL,
        source_ids TEXT NOT NULL,
        source_fingerprint TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        decided_by TEXT,
        decided_at TEXT,
        decision_reason TEXT,
        flags_at_approval INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_visit_summaries_patient
        ON visit_summaries (patient_id, status);
    -- one draft per patient. two would mean two people reviewing different
    -- drafts of the same thing, and one approval silently losing to the other
    CREATE UNIQUE INDEX IF NOT EXISTS idx_visit_summaries_one_draft
        ON visit_summaries (patient_id) WHERE status = 'draft';
    CREATE TABLE IF NOT EXISTS visit_summary_versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        summary_id INTEGER NOT NULL REFERENCES visit_summaries(id),
        version INTEGER NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('generated', 'edited')),
        body TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (summary_id, version)
    );
    -- append-only, the same way the audit trail is: only erasure, which
    -- writes audit_unlock inside its own transaction, may remove a version
    CREATE TRIGGER IF NOT EXISTS visit_summary_versions_no_update
        BEFORE UPDATE ON visit_summary_versions
        BEGIN SELECT RAISE(ABORT, 'summary versions are append-only'); END;
    CREATE TRIGGER IF NOT EXISTS visit_summary_versions_no_delete
        BEFORE DELETE ON visit_summary_versions
        WHEN NOT EXISTS (SELECT 1 FROM audit_unlock)
        BEGIN SELECT RAISE(ABORT, 'summary versions are append-only'); END;
"""


class SummaryError(ValueError):
    """A workflow step refused. `code` is from a closed list."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class NothingToSummarise(SummaryError):
    def __init__(self):
        super().__init__("no_sources", "this patient has no notes to summarise")


class SummaryFailed(SummaryError):
    def __init__(self):
        super().__init__("generator_failed", "the summary could not be built; nothing was saved")


# --- sources ---------------------------------------------------------------

def _visit_rows(conn, pid):
    return conn.execute(
        "SELECT id, visit_date, procedures, clinical_notes, next_appointment, source_path"
        " FROM visits WHERE patient_id = ? ORDER BY id", (pid,)).fetchall()


def fingerprint(conn, pid):
    """Every visit the patient has, as it is now. A summary whose fingerprint
    differs was made from notes that have since changed, or been added to."""
    rows = [tuple(r) for r in _visit_rows(conn, pid)]
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def _filed_by(conn, source_path):
    """Who filed the note, from the audit trail - or None. Never a guess.

    This is the person or process that filed it, not necessarily the clinician
    who wrote it: the visit row has no author column. The page says so.
    """
    row = conn.execute(
        "SELECT username, ts FROM audit_log WHERE allowed = 1"
        " AND action IN ('sync_note', 'append_note', 'upload_file')"
        " AND target IN (?, ?) ORDER BY id LIMIT 1",
        (source_path, f"sorted/{source_path}")).fetchone()
    return (row["username"], row["ts"]) if row else (None, None)


def sources(conn, pid):
    """The patient's most recent visits, newest first, undated last."""
    rows = _visit_rows(conn, pid)
    dated = sorted((r for r in rows if r["visit_date"]),
                   key=lambda r: (r["visit_date"], r["id"]), reverse=True)
    undated = [r for r in rows if not r["visit_date"]]
    out = []
    for r in (dated + undated)[:MAX_SOURCES]:
        try:
            procedures = [p for p in json.loads(r["procedures"] or "[]") if str(p).strip()]
        except ValueError:
            procedures = []
        filed_by, filed_at = _filed_by(conn, r["source_path"])
        out.append({"id": r["id"], "visit_date": r["visit_date"], "procedures": procedures,
                    "clinical_notes": r["clinical_notes"] or "",
                    "next_appointment": r["next_appointment"] or "",
                    "source_path": r["source_path"], "filed_by": filed_by,
                    "filed_at": filed_at})
    return out, len(rows)


# --- the generator ---------------------------------------------------------

TOOTH = re.compile(r"\b([1-4][1-8])\b")
EXTRACTION = re.compile(r"\b(ext|extraction|estrazione)\b", re.I)


def _unknown_codes(procedures):
    from dental_notes_schema import KNOWN_PROCEDURES
    out = []
    for entry in procedures:
        code = str(entry).strip().split(" ")[0].lower()
        if code and code not in KNOWN_PROCEDURES:
            out.append(code)
    return out


def extractive(srcs):
    """Lines restated from fields and quoted from notes. Nothing else."""
    srcs, total = srcs
    lines = []

    def add(kind, text, cited):
        lines.append({"kind": kind, "text": text, "sources": list(cited)})

    for s in srcs:
        ref = f"[#{s['id']}]"
        procs = ", ".join(str(p) for p in s["procedures"])
        if s["visit_date"]:
            if procs:
                add("fact", f"{s['visit_date']}: procedures {procs} {ref}", [s["id"]])
            else:
                add("gap", f"Visit {ref} on {s['visit_date']}: no procedures recorded", [s["id"]])
        else:
            add("gap", f"Visit {ref} has no date", [s["id"]])
            if procs:
                add("fact", f"Undated visit: procedures {procs} {ref}", [s["id"]])
            else:
                add("gap", f"Visit {ref}: no procedures recorded", [s["id"]])
        for code in _unknown_codes(s["procedures"]):
            add("gap", f"Visit {ref}: procedure code '{code}' is not in the glossary", [s["id"]])
        if s["clinical_notes"].strip():
            add("quote", f'Note {ref}: "{s["clinical_notes"]}"', [s["id"]])
        else:
            add("gap", f"Visit {ref}: no note text", [s["id"]])

    latest = next((s for s in srcs if s["next_appointment"]), None)
    if latest:
        add("quote", f'Next appointment written as "{latest["next_appointment"]}" '
                     f'[#{latest["id"]}]', [latest["id"]])

    lines.extend(_conflicts(srcs))
    if total > len(srcs):
        add("gap", f"{total - len(srcs)} older visits are not included", [])
    return lines


def _conflicts(srcs):
    """Things two notes say that cannot both be the whole story."""
    out = []
    dated = sorted((s for s in srcs if s["visit_date"]), key=lambda s: (s["visit_date"], s["id"]))
    for i, first in enumerate(dated):
        for proc in first["procedures"]:
            if not EXTRACTION.search(str(proc)):
                continue
            for tooth in TOOTH.findall(str(proc)):
                for later in dated[i + 1:]:
                    for other in later["procedures"]:
                        if tooth in TOOTH.findall(str(other)) and not EXTRACTION.search(str(other)):
                            out.append({"kind": "conflict", "sources": [first["id"], later["id"]],
                                        "text": f"Possible contradiction: tooth {tooth} extracted"
                                                f" on {first['visit_date']} [#{first['id']}] but"
                                                f" {other} on {later['visit_date']}"
                                                f" [#{later['id']}]"})
    by_date = {}
    for s in dated:
        by_date.setdefault(s["visit_date"], []).append(s)
    for day, same in by_date.items():
        if len(same) > 1:
            refs = " ".join(f"[#{s['id']}]" for s in same)
            out.append({"kind": "conflict", "sources": [s["id"] for s in same],
                        "text": f"Two records are dated {day} {refs}: check they are the same"
                                f" visit"})
    return out


# --- the support check -----------------------------------------------------

CITE = re.compile(r"\[#(\d+)\]")
# greedy on purpose: a note may itself contain a double quote, and the quote
# a line carries runs from its first mark to its last
QUOTED = re.compile(r'"(.*)"')
WORD = re.compile(r"[a-zà-ù]{3,}")
NUMBER = re.compile(r"\d{4}-\d{2}-\d{2}|\d+")
NEGATIONS = {"no", "non", "not", "nessun", "nessuna", "nessuno", "senza", "without", "never",
             "mai", "assenza"}
RECOMMEND = re.compile(r"prescri|recommend|raccomand|consigli|should|dovrebbe|diagnos|therap"
                       r"|terapi|must take|da assumere|assumere", re.I)
# the words the generator's own templates use, plus function words. a content
# word outside this list must be found in the cited notes.
TEMPLATE_WORDS = {
    "procedures", "procedure", "visit", "visits", "undated", "has", "date", "recorded", "note",
    "text", "code", "the", "glossary", "not", "next", "appointment", "written", "possible",
    "contradiction", "tooth", "extracted", "but", "two", "records", "are", "dated", "check",
    "they", "same", "older", "included", "and", "with", "for", "was", "were", "per", "del",
    "della", "dei", "con", "alla", "gap",
}


def _cited_text(cited):
    parts = []
    for s in cited:
        parts += [s["visit_date"] or "", " ".join(str(p) for p in s["procedures"]),
                  s["clinical_notes"], s["next_appointment"]]
    return " ".join(parts)


def _negated_words(text):
    tokens = re.findall(r"[a-zà-ù]+", text.lower())
    out = set()
    for i, tok in enumerate(tokens):
        if tok in NEGATIONS:
            out.update(tokens[i + 1:i + 3])
    return out


def check_line(line, by_id):
    """-> the flags on one line. Empty means every claim is in its sources."""
    flags = []
    cited_ids = set(line.get("sources") or []) | {int(n) for n in CITE.findall(line["text"])}
    if any(i not in by_id for i in cited_ids):
        flags.append("unknown_source")
    cited = [by_id[i] for i in sorted(cited_ids) if i in by_id]
    if not cited and line["kind"] != "gap":
        flags.append("no_source")
    source_text = _cited_text(cited)

    outside = CITE.sub(" ", line["text"])
    for quote in QUOTED.findall(outside):
        if quote and quote not in source_text:
            flags.append("misquote")
    outside = QUOTED.sub(" ", outside)

    if RECOMMEND.search(outside):
        flags.append("recommendation")
    if line["kind"] == "gap" or not cited:
        return sorted(set(flags))

    lower_source = source_text.lower()
    source_words = set(re.findall(r"[a-zà-ù]+", lower_source))
    for number in NUMBER.findall(outside):
        if not re.search(rf"(?<![\d-]){re.escape(number)}(?![\d-])", source_text):
            flags.append("unsupported")
    for word in WORD.findall(outside.lower()):
        if word not in TEMPLATE_WORDS and word not in NEGATIONS and word not in source_words:
            flags.append("unsupported")

    line_tokens = set(re.findall(r"[a-zà-ù]+", outside.lower()))
    line_negated = bool(line_tokens & NEGATIONS) and line["kind"] == "fact"
    if line_negated and not (source_words & NEGATIONS):
        flags.append("negation")
    if not line_negated and (line_tokens - TEMPLATE_WORDS) & _negated_words(source_text):
        flags.append("negation")
    return sorted(set(flags))


def check_lines(lines, srcs):
    srcs = srcs[0] if isinstance(srcs, tuple) else srcs
    by_id = {s["id"]: s for s in srcs}
    return [dict(line, flags=check_line(line, by_id)) for line in lines]


# --- editing ---------------------------------------------------------------

def render_text(lines):
    return "\n".join(line["text"] for line in lines)


def parse_edit(text):
    """A clinician's edit, one line per statement, citations kept as [#id]."""
    out = []
    for raw in (text or "").splitlines()[:MAX_LINES]:
        raw = raw.strip()[:MAX_LINE_CHARS]
        if not raw:
            continue
        if raw.startswith("Note [#") or raw.startswith("Next appointment written as"):
            kind = "quote"
        elif raw.startswith("Possible contradiction") or raw.startswith("Two records"):
            kind = "conflict"
        elif (raw.startswith("Visit [#") or "older visits are not included" in raw):
            kind = "gap"
        else:
            kind = "fact"
        out.append({"kind": kind, "text": raw, "sources": [int(n) for n in CITE.findall(raw)]})
    return out


# --- workflow --------------------------------------------------------------

def _require(conn, actor, role, action, target):
    if not authorize(role, CAPABILITY):
        log_audit(conn, actor, role, action, target, allowed=0)
        raise PermissionError(f"{role} may not {action.replace('_', ' ')}")


def _own(conn, sid, pid, actor, role, action):
    """The summary, if it is this patient's. Anything else is not found."""
    _require(conn, actor, role, action, f"summary:{sid}")
    row = conn.execute("SELECT * FROM visit_summaries WHERE id = ? AND patient_id = ?",
                       (sid, pid)).fetchone()
    if row is None:
        log_audit(conn, actor, role, action, f"summary:{sid}", allowed=0)
        raise LookupError("no such summary for this patient")
    return row


def _latest(conn, sid):
    return conn.execute("SELECT * FROM visit_summary_versions WHERE summary_id = ?"
                        " ORDER BY version DESC LIMIT 1", (sid,)).fetchone()


def generate(conn, pid, actor, role, now=None, generator=None, regenerate=False):
    """-> the id of this patient's draft. An existing draft is returned as is."""
    _require(conn, actor, role, "summary_generate", f"patient:{pid}")
    now = now or clinic_time.now_utc()
    srcs = sources(conn, pid)
    if not srcs[0]:
        raise NothingToSummarise()
    existing = conn.execute("SELECT id FROM visit_summaries WHERE patient_id = ?"
                            " AND status = 'draft'", (pid,)).fetchone()
    if existing and not regenerate:
        return existing[0]

    # build everything before writing anything: a generator that fails leaves
    # no row behind, and it only ever read the visits
    try:
        lines = (generator or extractive)(srcs)
        lines = check_lines(lines, srcs)
        for line in lines:
            if line["kind"] not in KINDS:
                raise ValueError(f"unknown line kind {line['kind']!r}")
    except Exception:
        log_audit(conn, actor, role, "summary_generate_failed", f"patient:{pid}", allowed=1)
        raise SummaryFailed() from None

    stamp = clinic_time.to_storage(now)
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute("SELECT id FROM visit_summaries WHERE patient_id = ?"
                                " AND status = 'draft'", (pid,)).fetchone()
        if existing and not regenerate:
            # someone else's click got here first; theirs is the draft
            conn.execute("COMMIT")
            return existing[0]
        if existing:
            conn.execute("UPDATE visit_summaries SET status = 'superseded', decided_by = ?,"
                         " decided_at = ?, decision_reason = 'regenerated' WHERE id = ?",
                         (actor, stamp, existing[0]))
        cur = conn.execute(
            "INSERT INTO visit_summaries (patient_id, status, generator, generator_version,"
            " source_ids, source_fingerprint, created_by, created_at)"
            " VALUES (?, 'draft', ?, ?, ?, ?, ?, ?)",
            (pid, GENERATOR if generator is None else getattr(generator, "__name__", "custom"),
             GENERATOR_VERSION, json.dumps([s["id"] for s in srcs[0]]),
             fingerprint(conn, pid), actor, stamp))
        sid = cur.lastrowid
        conn.execute("INSERT INTO visit_summary_versions (summary_id, version, kind, body,"
                     " created_by, created_at) VALUES (?, 1, 'generated', ?, ?, ?)",
                     (sid, json.dumps({"lines": lines}), actor, stamp))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    log_audit(conn, actor, role, "summary_regenerate" if regenerate else "summary_generate",
              f"summary:{sid}", allowed=1)
    return sid


def regenerate(conn, sid, pid, actor, role, now=None):
    row = _own(conn, sid, pid, actor, role, "summary_regenerate")
    if row["status"] != "draft":
        raise SummaryError("not_draft", "only a draft can be regenerated")
    return generate(conn, pid, actor, role, now=now, regenerate=True)


def edit(conn, sid, pid, text, actor, role, now=None):
    row = _own(conn, sid, pid, actor, role, "summary_edit")
    if row["status"] != "draft":
        raise SummaryError("not_draft", "only a draft can be edited")
    now = now or clinic_time.now_utc()
    lines = check_lines(parse_edit(text), sources(conn, pid))
    if not lines:
        raise SummaryError("empty", "an edit must keep at least one line")
    version = _latest(conn, sid)["version"] + 1
    conn.execute("INSERT INTO visit_summary_versions (summary_id, version, kind, body,"
                 " created_by, created_at) VALUES (?, ?, 'edited', ?, ?, ?)",
                 (sid, version, json.dumps({"lines": lines}), actor, clinic_time.to_storage(now)))
    conn.commit()
    log_audit(conn, actor, role, "summary_edit", f"summary:{sid}", allowed=1)
    return version


def approve(conn, sid, pid, actor, role, now=None):
    row = _own(conn, sid, pid, actor, role, "summary_approve")
    if row["status"] != "draft":
        raise SummaryError("not_draft", "only a draft can be approved")
    if fingerprint(conn, pid) != row["source_fingerprint"]:
        raise SummaryError("sources_changed",
                           "the notes changed after this draft was made; regenerate it")
    latest = _latest(conn, sid)
    lines = json.loads(latest["body"])["lines"]
    flagged = sum(1 for line in lines if line["flags"])
    if latest["kind"] == "generated" and flagged:
        # a machine claim the sources do not carry. a person must edit it out
        # or reject the draft; approving it as it stands is not offered
        raise SummaryError("flagged", f"{flagged} machine line(s) are not supported by the notes")
    now = clinic_time.to_storage(now or clinic_time.now_utc())
    conn.execute("UPDATE visit_summaries SET status = 'superseded' WHERE patient_id = ?"
                 " AND status = 'approved'", (pid,))
    conn.execute("UPDATE visit_summaries SET status = 'approved', decided_by = ?, decided_at = ?,"
                 " flags_at_approval = ? WHERE id = ?", (actor, now, flagged, sid))
    conn.commit()
    log_audit(conn, actor, role, "summary_approve", f"summary:{sid}", allowed=1)


def reject(conn, sid, pid, actor, role, reason="", now=None):
    row = _own(conn, sid, pid, actor, role, "summary_reject")
    if row["status"] != "draft":
        raise SummaryError("not_draft", "only a draft can be rejected")
    conn.execute("UPDATE visit_summaries SET status = 'rejected', decided_by = ?, decided_at = ?,"
                 " decision_reason = ? WHERE id = ?",
                 (actor, clinic_time.to_storage(now or clinic_time.now_utc()),
                  (reason or "")[:500], sid))
    conn.commit()
    log_audit(conn, actor, role, "summary_reject", f"summary:{sid}", allowed=1)


def load(conn, sid, pid, actor, role):
    """One summary with every version, its sources and whether it is outdated."""
    row = _own(conn, sid, pid, actor, role, "summary_read")
    log_audit(conn, actor, role, "summary_read", f"summary:{sid}", allowed=1)
    versions = []
    for v in conn.execute("SELECT * FROM visit_summary_versions WHERE summary_id = ?"
                          " ORDER BY version", (sid,)):
        versions.append({"version": v["version"], "kind": v["kind"],
                         "created_by": v["created_by"], "created_at": v["created_at"],
                         "lines": json.loads(v["body"])["lines"]})
    wanted = set(json.loads(row["source_ids"]))
    srcs = [s for s in sources(conn, pid)[0] if s["id"] in wanted]
    return {"summary": dict(row), "versions": versions, "sources": srcs,
            "missing_sources": sorted(wanted - {s["id"] for s in srcs}),
            "stale": fingerprint(conn, pid) != row["source_fingerprint"]}


def for_patient(conn, pid, limit=20):
    return conn.execute("SELECT * FROM visit_summaries WHERE patient_id = ?"
                        " ORDER BY id DESC LIMIT ?", (pid, limit)).fetchall()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        import visit_summary_selftest
        visit_summary_selftest.selftest()
        return
    print("usage: python visit_summary.py --selftest")
    sys.exit(1)


if __name__ == "__main__":
    main()
