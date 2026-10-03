"""Read an old shared folder without changing it, and propose - never decide - whose each file is (P25).

    python legacy_import.py --dry-run DIR [--clock-offset SECONDS]
    python legacy_import.py --stage DIR --as USERNAME [--clock-offset SECONDS]
    python legacy_import.py --reconcile [--apply]
    python legacy_import.py --selftest

THE SOURCE IS READ ONLY. Files are opened for reading with links refused; nothing
is moved, renamed, rewritten or deleted. A copy of each acceptable file is staged
under STAGING_ROOT by its sha256; that copy is what a dentist reviews and what
joins the record. This build reads only a folder carrying the synthetic-fixture
marker: a real shared folder needs the owner's separate authorisation (P25-D1).

EVIDENCE, NOT A VERDICT. Every clue says where it came from (page and line,
OCR, a path segment, EXIF, the file system). Deterministic gates turn clues into
one of: proposed (strong or needs checking), unmatched, conflict. No gate
attaches anything. A name, a date, a folder, a camera time or a filename never
makes a proposal strong; nothing here looks at faces, teeth or radiographs.

ONLY A DENTIST ATTACHES. confirm() rechecks, in one write transaction, that the
item is still undecided, the staged bytes still hash to what was reviewed, the
patient exists and a chosen visit is theirs; then the document is added to
P15's store as confirmed. Publication to the patient is a separate step
(patient_files.publish).

DICOM is recognised and refused: this build has no validated reader (P25-D2).
"""
import hashlib
import json
import os
import re
import sys
import tempfile
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import clinic_time
import codice_fiscale
import documents as docs
import patient_files
import patient_id
from auth import authorize, log_audit

MARKER = ".synthetic-legacy-fixture"
STAGING_ROOT = Path("import_staging")
# the only place the browser can import from: its direct sub-folders, picked by name (UIF)
INBOX = Path(os.environ.get("CLINIC_IMPORT_INBOX", "import_inbox"))
REVIEW = "read_clinical"
PROGRESS = "view_import_progress"
MAX_FILES = 5000
SKIP = {".DS_Store", "Thumbs.db", "desktop.ini", MARKER}
OPEN = ("proposed", "unmatched", "conflict", "held")
DICOM_REASON = "DICOM: this build cannot read the patient data inside it, so it was not read"
CLOCK_DAYS = 2

SCHEMA = """
    CREATE TABLE IF NOT EXISTS import_batches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_key TEXT NOT NULL,
        source_label TEXT NOT NULL,
        started_by TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL CHECK (status IN ('running', 'done')),
        clock_offset INTEGER NOT NULL DEFAULT 0,
        totals TEXT
    );
    CREATE TABLE IF NOT EXISTS import_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id INTEGER NOT NULL,
        source_key TEXT NOT NULL,
        rel_path TEXT NOT NULL,
        size INTEGER,
        mtime TEXT,
        sha256 TEXT,
        kind TEXT,
        state TEXT NOT NULL CHECK (state IN ('discovered', 'staged', 'proposed', 'unmatched',
            'conflict', 'held', 'confirmed', 'rejected', 'refused')),
        strength TEXT CHECK (strength IN ('strong', 'check')),
        -- the proposed patient while open, the confirmed one after
        patient_id TEXT,
        candidates TEXT,
        evidence TEXT,
        extraction TEXT,
        extractor TEXT,
        reason TEXT,
        document_id INTEGER,
        decided_by TEXT,
        decided_at TEXT,
        decision_reason TEXT,
        created_at TEXT NOT NULL
    );
    -- a re-run of the same folder finds the same file, never a second item
    CREATE UNIQUE INDEX IF NOT EXISTS idx_import_items_once
        ON import_items (source_key, rel_path, COALESCE(sha256, ''));
    CREATE INDEX IF NOT EXISTS idx_import_items_state ON import_items (state, batch_id);
    CREATE INDEX IF NOT EXISTS idx_import_items_patient ON import_items (patient_id);
    CREATE INDEX IF NOT EXISTS idx_import_items_sha ON import_items (sha256);
"""


class ImportProblem(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _now(now):
    return clinic_time.to_storage(now or clinic_time.now_utc())


def _require(conn, actor, role, capability, action, target):
    if not authorize(role, capability):
        log_audit(conn, actor, role, action, target, allowed=0)
        raise PermissionError(f"{role} may not {action.replace('_', ' ')}")


# --- the source folder ----------------------------------------------------------------

def _check_source(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ImportProblem("no_folder", "not a folder")
    if not (root / MARKER).is_file():
        raise ImportProblem("not_authorised", "This build imports only the synthetic test folder."
                            " Reading a real shared folder needs the owner's separate"
                            " authorisation (P25-D1).")
    return root.resolve()


def inbox_folders(inbox=None):
    """The import inbox's direct sub-folders, by name. Links, hidden folders and files are not offered."""
    root = Path(inbox or INBOX)
    if root.is_symlink() or not root.is_dir():
        return []
    out = []
    for path in sorted(root.iterdir()):
        if path.name.startswith(".") or path.is_symlink() or not path.is_dir():
            continue
        out.append({"name": path.name, "synthetic": (path / MARKER).is_file()})
    return out


def inbox_folder(name, inbox=None):
    """The folder the browser named - only one the inbox lists, so no path can be sent instead."""
    for folder in inbox_folders(inbox):
        if folder["name"] == name:
            return Path(inbox or INBOX) / name
    raise ImportProblem("no_folder", "That folder is not in the import inbox.")


def source_key(root):
    return hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()


def walk(root):
    """-> [(relative path, absolute path, refusal or None)], sorted, links never followed."""
    root = _check_source(root)
    out = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for d in list(dirnames):
            if os.path.islink(os.path.join(dirpath, d)):
                dirnames.remove(d)
                rel = os.path.relpath(os.path.join(dirpath, d), root)
                out.append((rel, None, "symbolic link to a folder - not followed"))
        for name in sorted(filenames):
            if name in SKIP or name.startswith("._"):
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root)
            if os.path.islink(path):
                out.append((rel, None, "symbolic link - not followed"))
            elif not os.path.isfile(path):
                out.append((rel, None, "not a regular file"))
            else:
                out.append((rel, path, None))
            if len(out) > MAX_FILES:
                raise ImportProblem("too_many", f"more than {MAX_FILES} files: split the folder")
    return out


def _read_source(path):
    """(bytes, size, mtime as stored text). Opened read-only; a link is refused by the OS."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        with os.fdopen(fd, "rb", closefd=False) as f:
            data = f.read(docs.MAX_BYTES + 1)
    finally:
        os.close(fd)
    mtime = datetime.fromtimestamp(st.st_mtime, timezone.utc)
    return data, st.st_size, clinic_time.to_storage(mtime)


def detect(data, name):
    if len(data) >= 132 and data[128:132] == b"DICM":
        return "dicom"
    return docs.detect(data, name)


def refusal(data, name, kind):
    if kind == "dicom":
        return DICOM_REASON
    return docs._refusal(data, name, kind)


# --- clues ---------------------------------------------------------------------------------

LABEL = re.compile(
    r"(?P<clinic_id>id\s+paziente|patient\s+id)"
    r"|(?P<cf>codice\s+fiscale|cod\.\s*fisc\.|c\.\s?f\.|\bcf\b)"
    r"|(?P<birth_date>data\s+di\s+nascita|nat[oa]\s+il|date\s+of\s+birth|birth\s+date|\bdob\b)"
    r"|(?P<doc_date>data\s+(?:esame|acquisizione|referto)|exam\s+date|study\s+date)"
    r"|(?P<name>cognome\s+e\s+nome|nome\s+e\s+cognome|paziente|patient|\bname\b)"
    r"\s*[:=]", re.IGNORECASE)
CF_TOKEN = re.compile(r"\b(?:[A-Z]{6}[0-9LMNPQRSTUV]{2}[A-Z][0-9LMNPQRSTUV]{2}[A-Z][0-9LMNPQRSTUV]{3}[A-Z]"
                      r"|[A-Z]{4}[0-9]{12})\b")
PID_TOKEN = re.compile(r"\bpid_[0-9a-f]{16}\b")
DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})|(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})")
TIME = re.compile(r"\b(\d{1,2})[:.](\d{2})\b")


def name_key(value):
    """Order, case and accents do not matter; at least two words or it is no name."""
    plain = unicodedata.normalize("NFKD", value or "")
    plain = "".join(ch for ch in plain if not unicodedata.combining(ch))
    words = re.findall(r"[a-z]+", plain.lower())
    return tuple(sorted(words)) if len(words) >= 2 else None


def parse_date(value):
    m = DATE.search(value or "")
    if not m:
        return None
    if m.group(1):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        d, mo, y = int(m.group(4)), int(m.group(5)), int(m.group(6))
    try:
        return datetime(y, mo, d).date().isoformat()
    except ValueError:
        return None


def born_compatible(code, day):
    """Does a birth date agree with what a real-shaped code encodes? None when it cannot say."""
    if not codice_fiscale.is_real(code or "") or not day:
        return None
    digits = "".join(codice_fiscale.OMOCODIA.get(c, c) for c in code[6:8] + code[9:11])
    month = "ABCDEHLMPRST".index(code[8]) + 1
    born = int(digits[2:])
    born = born - 40 if born > 40 else born
    y, m, d = day.split("-")
    return y[2:] == digits[:2] and int(m) == month and int(d) == born


class People:
    """The clinic's patients, read once per run, for exact lookups only."""

    def __init__(self, conn):
        self.conn = conn
        self.by_name, self.code = {}, {}
        for r in conn.execute("SELECT patient_id, codice_fiscale, patient_name FROM patients"):
            self.code[r[0]] = r[1]
            key = name_key(r[2])
            if key:
                self.by_name.setdefault(key, []).append(r[0])

    def identifier(self, kind, value):
        if kind == "clinic_id":
            return patient_id.resolve(self.conn, value) if patient_id.is_valid(value) else None
        return patient_id.resolve(self.conn, value)


def _identifier_clue(people, kind, value, source, where, labelled, confidence=None):
    if kind == "cf":
        valid = codice_fiscale.is_valid(value)
    else:
        valid = patient_id.is_valid(value)
    who = people.identifier(kind, value) if valid else None
    return {"type": kind, "value": value, "source": source, "where": where, "labelled": labelled,
            "confidence": confidence, "valid": valid, "patient": who}


def text_clues(people, pages, source):
    """Labelled fields and bare identifiers in extracted text. source: 'text' or 'ocr'."""
    clues = []
    for page in pages:
        conf = page.get("confidence") if source == "ocr" else None
        for n, line in enumerate(page["text"].splitlines() or [""], 1):
            where = (f"page {page['page']}, line {n}" if source == "text"
                     else f"image text read by OCR (confidence {conf}%)")
            labels = list(LABEL.finditer(line))
            seen = set()
            for i, m in enumerate(labels):
                kind = m.lastgroup
                end = labels[i + 1].start() if i + 1 < len(labels) else len(line)
                value = line[m.end():end].strip(" ,;")
                clue = _field(people, kind, value, source, where, conf)
                if clue:
                    clues.append(clue)
                    seen.add(clue["value"])
            upper = line.upper()
            for token in CF_TOKEN.findall(upper):
                if token not in seen:
                    clues.append(_identifier_clue(people, "cf", token, source, where, False, conf))
                    seen.add(token)
            for token in PID_TOKEN.findall(line):
                if token not in seen:
                    clues.append(_identifier_clue(people, "clinic_id", token, source, where, False, conf))
    return clues


def _field(people, kind, value, source, where, conf):
    if kind == "cf":
        m = CF_TOKEN.search(re.sub(r"\s+", "", value.upper()))
        return _identifier_clue(people, "cf", m.group(0), source, where, True, conf) if m else None
    if kind == "clinic_id":
        m = PID_TOKEN.search(value)
        return _identifier_clue(people, "clinic_id", m.group(0), source, where, True, conf) if m else None
    if kind in ("birth_date", "doc_date"):
        day = parse_date(value)
        if not day:
            return None
        clue = {"type": kind, "value": day, "source": source, "where": where, "labelled": True,
                "confidence": conf}
        t = TIME.search(value[DATE.search(value).end():])
        if kind == "doc_date" and t and int(t.group(1)) < 24 and int(t.group(2)) < 60:
            clue["time"] = f"{int(t.group(1)):02d}:{t.group(2)}"
        return clue
    name = re.sub(r"\s+", " ", value).strip()[:60]
    if not name_key(name):
        return None
    return {"type": "name", "value": name, "source": source, "where": where, "labelled": True,
            "confidence": conf, "patients": people.by_name.get(name_key(name), [])}


def path_clues(people, rel):
    clues, leads = [], []
    parts = Path(rel).parts
    for i, segment in enumerate(parts):
        last = i == len(parts) - 1
        where = f"file name '{segment}'" if last else f"folder '{segment}'"
        stem = Path(segment).stem if last else segment
        for token in re.split(r"[^A-Za-z0-9_]+", stem.upper()):
            for piece in token.split("_"):
                if CF_TOKEN.fullmatch(piece):
                    clues.append(_identifier_clue(people, "cf", piece, "path", where, False))
        for token in PID_TOKEN.findall(stem):
            clues.append(_identifier_clue(people, "clinic_id", token, "path", where, False))
        key = name_key(stem)
        if key and key in people.by_name:
            leads.append(f"{where} matches the name of {len(people.by_name[key])} patient(s);"
                         " a folder or file name is never enough")
    return clues, leads


# --- times -------------------------------------------------------------------------------

def exif_time(value, offset, clock_offset):
    """A camera time -> {'raw', 'utc', 'zone', 'corrected', 'problem'}. Never a guess."""
    out = {"raw": value, "utc": None, "zone": "", "corrected": False, "problem": None}
    try:
        local = datetime.strptime(value or "", "%Y:%m:%d %H:%M:%S")
    except ValueError:
        out["problem"] = "unreadable camera time"
        return out
    m = re.fullmatch(r"([+-])(\d{2}):(\d{2})", offset or "")
    if m:
        sign = 1 if m.group(1) == "+" else -1
        zone = timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3))))
        instant = local.replace(tzinfo=zone).astimezone(timezone.utc)
        out["zone"] = f"offset {offset} stated in EXIF"
    else:
        try:
            instant = clinic_time.to_utc(local)
        except clinic_time.AmbiguousLocalTime as e:
            out["problem"] = str(e)
            return out
        out["zone"] = f"EXIF has no time zone: clinic time ({clinic_time.zone_name()}) assumed"
    if clock_offset:
        instant += timedelta(seconds=clock_offset)
        out["corrected"] = True
    out["utc"] = instant.isoformat()
    return out


def _times(clues, exif, mtime, clock_offset, now):
    """-> (acquired_at, basis, times shown to the reviewer, flags)."""
    shown, flags = [], []
    doc = next((c for c in clues if c["type"] == "doc_date"), None)
    doc_utc = None
    if doc:
        local = datetime.fromisoformat(doc["value"] + "T" + doc.get("time", "12:00"))
        try:
            doc_utc = clinic_time.to_utc(local).isoformat()
        except clinic_time.AmbiguousLocalTime:
            doc_utc = None
        shown.append({"what": "date written on the document", "value": doc["value"] + (
            " " + doc["time"] if doc.get("time") else ""), "where": doc["where"]})
    camera = None
    if exif.get("original"):
        camera = exif_time(exif["original"], exif.get("offset"), clock_offset)
        label = "camera time (EXIF DateTimeOriginal)"
        if camera["corrected"]:
            label += f", corrected by {clock_offset} s declared for this batch"
        shown.append({"what": label, "value": exif["original"], "where": camera["zone"] or camera["problem"],
                      "utc": camera["utc"]})
        if camera["problem"]:
            flags.append(f"camera time not used: {camera['problem']}")
    if exif.get("modified"):
        shown.append({"what": "EXIF modified time - when software last saved it, not when it was taken",
                      "value": exif["modified"], "where": "EXIF DateTime"})
    if mtime:
        shown.append({"what": "file system modified time - not when it was taken",
                      "value": f"{clinic_time.local_date(mtime)} {clinic_time.local_hhmm(mtime)}",
                      "where": f"file system, clinic time ({clinic_time.zone_name()})"})
    if camera and camera["utc"]:
        taken = clinic_time.read_instant(camera["utc"])
        if taken.year < 1995 or taken > now + timedelta(days=1):
            flags.append("camera time is implausible - the device clock may be wrong")
        elif doc_utc and abs((taken - clinic_time.read_instant(doc_utc)).days) > CLOCK_DAYS:
            flags.append(f"camera time and the date on the document are {abs((taken - clinic_time.read_instant(doc_utc)).days)}"
                         " days apart - the device clock may be wrong")
        elif doc_utc is None and not camera["corrected"]:
            flags.append("camera time not checked against anything; treat it as unconfirmed")
    if doc_utc:
        return doc_utc, "document_date", shown, flags
    if camera and camera["utc"] and not any("clock" in f for f in flags):
        return camera["utc"], "exif_original", shown, flags
    return None, None, shown, flags


# --- the gates ---------------------------------------------------------------------------

def decide(people, clues):
    """-> (state, strength, patient, candidates, conflicts). The only place a result is chosen."""
    conflicts = []
    ids = [c for c in clues if c["type"] in ("cf", "clinic_id")]
    named = {c["patient"] for c in ids if c["valid"] and c["patient"]}
    unknown = [c for c in ids if c["valid"] and not c["patient"]]
    names = [c for c in clues if c["type"] == "name"]
    births = [c for c in clues if c["type"] == "birth_date"]
    candidates = set(named)
    for c in names:
        if c["source"] == "text" or c["patients"]:
            candidates.update(c["patients"])
    if len(named) > 1:
        conflicts.append("identifiers in this file name different patients")
    distinct = {name_key(c["value"]) for c in names if c["source"] == "text"}
    if len(distinct) > 1:
        conflicts.append("more than one person's name is written in this file")
    if unknown and (named or any(c["patients"] for c in names)):
        conflicts.extend(f"{c['value']} ({c['where']}) is a valid code but not a patient of this clinic"
                         for c in unknown)
    if len(named) == 1:
        who = next(iter(named))
        corroborated = False
        for c in names:
            if who in c["patients"]:
                corroborated = True
            elif c["patients"] or c["source"] == "text":
                conflicts.append(f"the name '{c['value']}' ({c['where']}) is not the name of the patient"
                                 " the code names")
        for c in births:
            fits = born_compatible(people.code.get(who), c["value"])
            if fits:
                corroborated = True
            elif fits is False:
                conflicts.append(f"the birth date {c['value']} ({c['where']}) does not match the code")
        kinds = {c["type"] for c in ids if c["valid"] and c["patient"] == who}
        if len(kinds) > 1:
            corroborated = True
        if conflicts:
            return "conflict", None, None, sorted(candidates), conflicts
        structured = any(c["patient"] == who and c["labelled"] and c["source"] == "text" for c in ids)
        return "proposed", "strong" if structured and corroborated else "check", who, sorted(candidates), []
    if conflicts:
        return "conflict", None, None, sorted(candidates), conflicts
    fitting = set()
    for c in names:
        fitting.update(c["patients"])
    if births and fitting:
        fitting = {p for p in fitting if any(born_compatible(people.code.get(p), b["value"]) for b in births)}
        if len(fitting) == 1:
            return "proposed", "check", next(iter(fitting)), sorted(candidates), []
        return "unmatched", None, None, sorted(candidates), []
    if len(fitting) > 1:
        return "conflict", None, None, sorted(candidates), [
            f"{len(fitting)} patients have this name and nothing in the file tells them apart"]
    return "unmatched", None, None, sorted(candidates), []


def examine(conn, people, rel, path, kind, mtime, sha, clock_offset=0, now=None):
    """Read one acceptable file and gather its evidence. Writes nothing. -> result dict."""
    now = now or clinic_time.now_utc()
    result = docs._extract(path, kind)
    code = result.get("error")
    if code in docs.QUARANTINE:
        return {"state": "refused", "reason": docs.QUARANTINE[code]}
    if code:
        return {"state": "staged", "reason": docs.FAILURE.get(code, "could not be read yet - run again")}
    pages = result["pages"]
    source = "ocr" if kind in ("png", "jpeg") else "text"
    clues = text_clues(people, pages, source)
    found, leads = path_clues(people, rel)
    clues += found
    state, strength, who, candidates, conflicts = decide(people, clues)
    twin = conn.execute("SELECT patient_id FROM patient_documents WHERE sha256 = ? AND status NOT IN"
                        " ('rejected', 'superseded')", (sha,)).fetchall()
    others = {r[0] for r in twin} - ({who} if who else set())
    if others and state == "proposed":
        state, strength, who = "conflict", None, None
        conflicts.append("these exact bytes are already in another patient's record")
    if others:
        candidates = sorted(set(candidates) | others)
    if state == "unmatched" and any(c["type"] == "name" and c["source"] == "text" for c in clues):
        leads.append("a name is written in the file but it does not identify one patient")
    acquired, basis, shown, flags = _times(clues, result.get("exif") or {}, mtime, clock_offset, now)
    evidence = {"clues": clues, "candidates": candidates, "conflicts": conflicts, "leads": leads,
                "times": shown, "flags": flags, "acquired_at": acquired, "acquired_basis": basis,
                "duplicates": []}
    return {"state": state, "strength": strength, "patient_id": who, "candidates": candidates,
            "evidence": evidence, "extraction": {"pages": pages}, "reason": None}


def cross_check(results):
    """The same bytes proposed for two different patients: every copy becomes a conflict.

    results: dicts with 'sha', 'rel', 'state', 'patient_id', 'evidence'. Changed in place."""
    groups = {}
    for r in results:
        if r.get("sha") and r["state"] in OPEN:
            groups.setdefault(r["sha"], []).append(r)
    for group in groups.values():
        if len(group) < 2:
            continue
        who = {r["patient_id"] for r in group if r["patient_id"]}
        for r in group:
            ev = r["evidence"]
            ev["duplicates"] = sorted({o["rel"] for o in group if o is not r})
            if len(who) > 1:
                r["state"], r["strength"], r["patient_id"] = "conflict", None, None
                ev["candidates"] = sorted(set(ev["candidates"]) | who)
                note = "the same bytes are also at another path proposed for a different patient"
                if note not in ev["conflicts"]:
                    ev["conflicts"].append(note)
    return results


# --- dry run and staging -----------------------------------------------------------------

def dry_run(conn, root, now=None, clock_offset=0):
    """Everything staging would find, with nothing written anywhere. -> report."""
    now = now or clinic_time.now_utc()
    people = People(conn)
    results = []
    for rel, path, refused in walk(root):
        entry = {"rel": rel, "kind": None, "sha": None, "state": "refused", "strength": None,
                 "patient_id": None, "reason": refused, "evidence": {"candidates": [], "conflicts": []}}
        if path:
            data, _size, mtime = _read_source(path)
            entry["sha"] = hashlib.sha256(data).hexdigest()
            entry["kind"] = detect(data, rel)
            entry["reason"] = refusal(data, rel, entry["kind"])
            if entry["reason"] is None:
                entry.update(examine(conn, people, rel, path, entry["kind"], mtime, entry["sha"],
                                     clock_offset, now))
        results.append(entry)
    cross_check(results)
    return {"totals": _totals([(r["state"], r["kind"]) for r in results]),
            "items": [{"rel": r["rel"], "kind": r["kind"], "result": r["state"], "strength": r["strength"],
                       "candidate": r["patient_id"], "reason": r["reason"]} for r in results]}


def _totals(pairs):
    by_state, by_kind = {}, {}
    for state, kind in pairs:
        by_state[state] = by_state.get(state, 0) + 1
        by_kind[kind or "unknown"] = by_kind.get(kind or "unknown", 0) + 1
    return {"files": len(pairs), "by_result": by_state, "by_type": by_kind}


def staged_file(r):
    return Path(STAGING_ROOT) / (r["sha256"] or "-")


def _stage_blob(sha, data):
    root = Path(STAGING_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    dest = root / sha
    if dest.exists():
        return
    fd, tmp = tempfile.mkstemp(dir=root, prefix=".stage-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.link(tmp, dest)
    except FileExistsError:
        pass
    finally:
        os.unlink(tmp)


def stage(conn, root, actor, role, now=None, clock_offset=0):
    """Stage every acceptable file and gather its evidence. Resumes an interrupted batch. -> batch id."""
    _require(conn, actor, role, PROGRESS, "import_stage", "import")
    files = walk(root)
    now = now or clinic_time.now_utc()
    key = source_key(root)
    running = conn.execute("SELECT id FROM import_batches WHERE source_key = ? AND status = 'running'"
                           " ORDER BY id DESC LIMIT 1", (key,)).fetchone()
    done = _already_imported(conn, key, files)
    if running:
        batch = running[0]
    elif done:
        # the same folder, unchanged: the same import (a second click must not add an empty run)
        log_audit(conn, actor, role, "import_stage", f"batch:{done}", allowed=1)
        return done
    else:
        batch = conn.execute("INSERT INTO import_batches (source_key, source_label, started_by, started_at,"
                             " status, clock_offset) VALUES (?, ?, ?, ?, 'running', ?)",
                             (key, Path(root).resolve().name[:60], actor, _now(now), clock_offset)).lastrowid
        conn.commit()
    log_audit(conn, actor, role, "import_stage", f"batch:{batch}", allowed=1)
    people = People(conn)
    for rel, path, refused in files:
        item_id = _discover(conn, batch, key, rel, path, refused, now)
        r = item(conn, item_id)
        if r["state"] in ("discovered", "staged"):
            _assess(conn, people, r, clock_offset, now)
    _recheck_duplicates(conn)
    totals = _totals([(r["state"], r["kind"]) for r in items(conn, batch)])
    conn.execute("UPDATE import_batches SET status = 'done', finished_at = ?, totals = ? WHERE id = ?",
                 (_now(now), json.dumps(totals), batch))
    conn.commit()
    return batch


def _already_imported(conn, key, files):
    """-> the last finished run of this folder if every file in it now is already an item, else None."""
    last = conn.execute("SELECT id FROM import_batches WHERE source_key = ? AND status = 'done'"
                        " ORDER BY id DESC LIMIT 1", (key,)).fetchone()
    if last is None:
        return None
    for rel, path, refused in files:
        if refused:
            seen = conn.execute("SELECT 1 FROM import_items WHERE source_key = ? AND rel_path = ? AND sha256 IS NULL",
                                (key, rel)).fetchone()
        else:
            sha = hashlib.sha256(_read_source(path)[0]).hexdigest()
            seen = conn.execute("SELECT 1 FROM import_items WHERE source_key = ? AND rel_path = ? AND sha256 = ?",
                                (key, rel, sha)).fetchone()
        if seen is None:
            return None
    return last[0]


def _discover(conn, batch, key, rel, path, refused, now):
    """One row per file; its staged copy exists before the row says 'staged'."""
    if refused:
        conn.execute("INSERT OR IGNORE INTO import_items (batch_id, source_key, rel_path, state, reason,"
                     " created_at) VALUES (?, ?, ?, 'refused', ?, ?)", (batch, key, rel, refused, _now(now)))
        conn.commit()
        return conn.execute("SELECT id FROM import_items WHERE source_key = ? AND rel_path = ? AND sha256"
                            " IS NULL", (key, rel)).fetchone()[0]
    data, size, mtime = _read_source(path)
    sha = hashlib.sha256(data).hexdigest()
    kind = detect(data, rel)
    reason = refusal(data, rel, kind)
    existing = conn.execute("SELECT id FROM import_items WHERE source_key = ? AND rel_path = ? AND sha256 = ?",
                            (key, rel, sha)).fetchone()
    if existing:
        return existing[0]
    if reason is None:
        _stage_blob(sha, data)
    conn.execute("INSERT OR IGNORE INTO import_items (batch_id, source_key, rel_path, size, mtime, sha256,"
                 " kind, state, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (batch, key, rel, size, mtime, sha, kind, "refused" if reason else "staged", reason, _now(now)))
    conn.commit()
    return conn.execute("SELECT id FROM import_items WHERE source_key = ? AND rel_path = ? AND sha256 = ?",
                        (key, rel, sha)).fetchone()[0]


def _assess(conn, people, r, clock_offset, now):
    out = examine(conn, people, r["rel_path"], staged_file(r), r["kind"], r["mtime"], r["sha256"],
                  clock_offset, now)
    if out["state"] in ("refused", "staged"):
        conn.execute("UPDATE import_items SET state = ?, reason = ? WHERE id = ? AND state IN"
                     " ('discovered', 'staged')", (out["state"], out["reason"], r["id"]))
    else:
        conn.execute("UPDATE import_items SET state = ?, strength = ?, patient_id = ?, candidates = ?,"
                     " evidence = ?, extraction = ?, extractor = ?, reason = NULL WHERE id = ? AND state IN"
                     " ('discovered', 'staged')",
                     (out["state"], out["strength"], out["patient_id"], json.dumps(out["candidates"]),
                      json.dumps(out["evidence"]), json.dumps(out["extraction"]), docs.EXTRACTOR, r["id"]))
    conn.commit()


def _recheck_duplicates(conn):
    rows = conn.execute("SELECT * FROM import_items WHERE state IN ('proposed', 'unmatched', 'conflict',"
                        " 'held') AND sha256 IN (SELECT sha256 FROM import_items WHERE sha256 IS NOT NULL"
                        " AND state IN ('proposed', 'unmatched', 'conflict', 'held') GROUP BY sha256"
                        " HAVING COUNT(*) > 1)").fetchall()
    results = [{"id": r["id"], "sha": r["sha256"], "rel": r["rel_path"], "state": r["state"],
                "strength": r["strength"], "patient_id": r["patient_id"], "evidence": evidence(r)} for r in rows]
    for r in cross_check(results):
        conn.execute("UPDATE import_items SET state = ?, strength = ?, patient_id = ?, candidates = ?,"
                     " evidence = ? WHERE id = ? AND state IN ('proposed', 'unmatched', 'conflict', 'held')",
                     (r["state"], r["strength"], r["patient_id"], json.dumps(r["evidence"]["candidates"]),
                      json.dumps(r["evidence"]), r["id"]))
    conn.commit()


# --- reading the queue ------------------------------------------------------------------

def item(conn, item_id):
    return conn.execute("SELECT * FROM import_items WHERE id = ?", (item_id,)).fetchone()


def items(conn, batch_id, state=None):
    sql, args = "SELECT * FROM import_items WHERE 1 = 1", []
    if batch_id is not None:
        sql, args = sql + " AND batch_id = ?", args + [batch_id]
    if state:
        sql, args = sql + " AND state = ?", args + [state]
    return conn.execute(sql + " ORDER BY id LIMIT 5000", args).fetchall()


def evidence(r):
    ev = json.loads(r["evidence"] or "{}")
    for k in ("clues", "candidates", "conflicts", "leads", "times", "flags", "duplicates"):
        ev.setdefault(k, [])
    ev.setdefault("acquired_at", None)
    ev.setdefault("acquired_basis", None)
    return ev


def progress(conn, actor, role):
    """Batch totals by state and type. No names, paths, identifiers or contents."""
    _require(conn, actor, role, PROGRESS, "import_progress", "import")
    out = []
    for b in conn.execute("SELECT id, started_at, finished_at, status FROM import_batches ORDER BY id DESC"
                          " LIMIT 50").fetchall():
        counts = {r[0]: r[1] for r in conn.execute("SELECT state, COUNT(*) FROM import_items WHERE batch_id = ?"
                                                   " GROUP BY state", (b["id"],))}
        types = {(r[0] or "unknown"): r[1] for r in conn.execute(
            "SELECT kind, COUNT(*) FROM import_items WHERE batch_id = ? GROUP BY kind", (b["id"],))}
        out.append({"batch": b["id"], "started_at": b["started_at"], "finished_at": b["finished_at"],
                    "status": b["status"], "counts": counts, "types": types})
    return out


def queue(conn, actor, role, state=None, limit=200):
    _require(conn, actor, role, REVIEW, "import_queue", "import")
    states = (state,) if state in OPEN else OPEN
    marks = ",".join("?" * len(states))
    return conn.execute(f"SELECT * FROM import_items WHERE state IN ({marks}) ORDER BY"
                        " CASE state WHEN 'conflict' THEN 0 WHEN 'proposed' THEN 1 WHEN 'unmatched' THEN 2"
                        " ELSE 3 END, id LIMIT ?", (*states, limit)).fetchall()


def load_item(conn, item_id, actor, role):
    _require(conn, actor, role, REVIEW, "import_item_read", f"item:{item_id}")
    r = item(conn, item_id)
    if r is None:
        raise LookupError("no such item")
    log_audit(conn, actor, role, "import_item_read", f"item:{item_id}", allowed=1)
    return r


def staged_bytes(r):
    """The staged copy, only if it still hashes to what was recorded."""
    path = staged_file(r)
    if not r["sha256"] or path.is_symlink() or not path.is_file():
        return None
    data = path.read_bytes()
    return data if hashlib.sha256(data).hexdigest() == r["sha256"] else None


def suggested_visits(conn, r, pid):
    """Visits of this patient on the evidenced date. A suggestion; never applied by itself."""
    when = evidence(r)["acquired_at"]
    if not when or not pid:
        return []
    day = clinic_time.local_date(when)
    return [dict(v) for v in conn.execute("SELECT id, visit_date, procedures FROM visits WHERE"
                                          " patient_id = ? AND visit_date = ? ORDER BY id", (pid, day))]


# --- decisions ------------------------------------------------------------------------------

def _resolve(conn, key):
    key = (key or "").strip()
    return patient_id.resolve(conn, key) or patient_id.resolve(conn, codice_fiscale.normalize(key))


def confirm(conn, item_id, patient_key, actor, role, expected_sha, reason="", visit_id=None,
            category=None, now=None):
    """A dentist says whose this file is. The only way an imported file joins a record. -> document id."""
    _require(conn, actor, role, REVIEW, "import_confirm", f"item:{item_id}")
    reason = (reason or "").strip()[:300]
    if category and category not in patient_files.CATEGORIES:
        raise ImportProblem("category", "choose a category from the list")
    pid = _resolve(conn, patient_key)
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    created = None
    try:
        r = item(conn, item_id)
        if r is None:
            raise LookupError("no such item")
        if r["state"] in ("confirmed", "rejected"):
            raise ImportProblem("decided", "this file has already been decided")
        if r["state"] not in OPEN:
            raise ImportProblem("not_reviewable", "this file cannot be attached")
        if pid is None:
            raise ImportProblem("no_patient", "no patient with that code")
        if pid != r["patient_id"] and not reason:
            raise ImportProblem("reason_needed", "say why: this is not the proposed patient")
        data = staged_bytes(r)
        if expected_sha != r["sha256"] or data is None:
            raise ImportProblem("changed", "the file is not the one that was reviewed; open it again")
        if visit_id and conn.execute("SELECT 1 FROM visits WHERE id = ? AND patient_id = ?",
                                     (visit_id, pid)).fetchone() is None:
            raise ImportProblem("visit", "that visit is not this patient's")
        doc_id, created = _link_document(conn, r, pid, data, actor, visit_id or None, category or None,
                                         reason, now)
        _mark_confirmed(conn, r, pid, doc_id, actor, reason, now)
        conn.commit()
    except BaseException:
        conn.rollback()
        if created:
            created.unlink(missing_ok=True)
        raise
    try:
        docs._index(docs.row(conn, doc_id))
    except docs.DocumentError:
        pass
    log_audit(conn, actor, role, "import_confirm", f"item:{item_id}", allowed=1, reason=f"document:{doc_id}")
    return doc_id


def _link_document(conn, r, pid, data, actor, visit_id, category, reason, now):
    """-> (document id, path this call created or None). Inside confirm's transaction."""
    same = conn.execute("SELECT id, status FROM patient_documents WHERE patient_id = ? AND sha256 = ?"
                        " AND status NOT IN ('rejected', 'superseded')", (pid, r["sha256"])).fetchone()
    if same and same["status"] == "confirmed":
        return same["id"], None
    if same:
        raise ImportProblem("waiting", "the same file is already waiting for review in this record")
    created = Path(docs.DOC_ROOT) / pid / r["sha256"] if docs._store(pid, r["sha256"], data) else None
    ev = evidence(r)
    starter = conn.execute("SELECT started_by FROM import_batches WHERE id = ?", (r["batch_id"],)).fetchone()
    cur = conn.execute(
        "INSERT INTO patient_documents (patient_id, kind, display_name, stored_path, sha256, size, status,"
        " extraction, extractor, uploaded_by, uploaded_at, decided_by, decided_at, category, source,"
        " import_item_id, acquired_at, acquired_basis, visit_id)"
        " VALUES (?, ?, ?, ?, ?, ?, 'confirmed', ?, ?, ?, ?, ?, ?, ?, 'legacy_import', ?, ?, ?, ?)",
        (pid, r["kind"], docs.display_name(Path(r["rel_path"]).name), f"{pid}/{r['sha256']}", r["sha256"],
         len(data), r["extraction"], r["extractor"], starter[0] if starter else actor, r["created_at"],
         actor, _now(now), category, r["id"], ev["acquired_at"], ev["acquired_basis"], visit_id))
    patient_files._event(conn, cur.lastrowid, "attached", None, pid, actor, now, reason or None)
    return cur.lastrowid, created


def _mark_confirmed(conn, r, pid, doc_id, actor, reason, now):
    changed = conn.execute("UPDATE import_items SET state = 'confirmed', patient_id = ?, document_id = ?,"
                           " decided_by = ?, decided_at = ?, decision_reason = ? WHERE id = ? AND state = ?",
                           (pid, doc_id, actor, _now(now), reason or None, r["id"], r["state"])).rowcount
    if not changed:
        raise ImportProblem("decided", "this file has already been decided")


def _decide_open(conn, item_id, to_state, reason, actor, role, action, now):
    _require(conn, actor, role, REVIEW, action, f"item:{item_id}")
    reason = (reason or "").strip()[:300]
    if not reason:
        raise ImportProblem("reason_needed", "say why")
    changed = conn.execute(f"UPDATE import_items SET state = ?, decided_by = ?, decided_at = ?,"
                           f" decision_reason = ? WHERE id = ? AND state IN ({','.join('?' * len(OPEN))})",
                           (to_state, actor, _now(now), reason, item_id, *OPEN)).rowcount
    conn.commit()
    if not changed:
        raise ImportProblem("decided", "this file has already been decided")
    log_audit(conn, actor, role, action, f"item:{item_id}", allowed=1)


def reject(conn, item_id, reason, actor, role, now=None):
    """Not this clinic's record for anyone: nothing is attached. The source file stays where it is."""
    _decide_open(conn, item_id, "rejected", reason, actor, role, "import_reject", now)


def hold(conn, item_id, reason, actor, role, now=None):
    """Kept in the queue for investigation. Can still be confirmed or rejected later."""
    _decide_open(conn, item_id, "held", reason, actor, role, "import_hold", now)


# --- life cycle ---------------------------------------------------------------------------

def forget_patient(conn, pid):
    """Inside an erasure transaction: every staged item that names this patient. -> their hashes."""
    rows = conn.execute("SELECT id, sha256 FROM import_items WHERE patient_id = ? OR EXISTS (SELECT 1 FROM"
                        " json_each(COALESCE(import_items.candidates, '[]')) WHERE value = ?)",
                        (pid, pid)).fetchall()
    conn.executemany("DELETE FROM import_items WHERE id = ?", [(r[0],) for r in rows])
    return {r[1] for r in rows if r[1]}


def remove_unreferenced(conn, shas):
    """After the erasure commits: staged copies no remaining item points to."""
    root = Path(STAGING_ROOT).resolve()
    for sha in shas:
        if conn.execute("SELECT 1 FROM import_items WHERE sha256 = ?", (sha,)).fetchone():
            continue
        path = Path(STAGING_ROOT) / sha
        if path.is_file() and root in path.resolve().parents:
            path.unlink()


def reconcile(conn, apply=False):
    """Staged copies against their rows. Missing or changed copies are reported, never repaired."""
    root = Path(STAGING_ROOT)
    shas = {r[0] for r in conn.execute("SELECT sha256 FROM import_items WHERE sha256 IS NOT NULL"
                                       " AND state != 'refused'")}
    orphans, temps = [], []
    for path in sorted(root.iterdir()) if root.is_dir() else []:
        if path.is_symlink() or not path.is_file():
            continue
        if path.name.startswith(".stage-"):
            temps.append(path)
        elif path.name not in shas:
            orphans.append(path)
    missing, damaged = [], []
    for r in conn.execute("SELECT id, sha256, state FROM import_items WHERE sha256 IS NOT NULL AND state IN"
                          " ('staged', 'proposed', 'unmatched', 'conflict', 'held')"):
        path = root / r["sha256"]
        if not path.is_file():
            missing.append(r["id"])
        elif hashlib.sha256(path.read_bytes()).hexdigest() != r["sha256"]:
            damaged.append(r["id"])
    if apply:
        for path in orphans + temps:
            path.unlink()
    return {"orphan_files": len(orphans), "temp_files": len(temps), "missing": missing, "damaged": damaged,
            "applied": apply}


# --- command line --------------------------------------------------------------------------

def _arg(argv, name):
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else None


def main(argv):
    from storage import connect
    if argv[:1] == ["--selftest"]:
        import legacy_import_selftest
        legacy_import_selftest.selftest()
        return 0
    offset = int(_arg(argv, "--clock-offset") or 0)
    conn = connect("db/clinic.sqlite")
    try:
        if argv[:1] == ["--dry-run"] and len(argv) > 1:
            print(json.dumps(dry_run(conn, argv[1], clock_offset=offset), indent=2))
            return 0
        if argv[:1] == ["--stage"] and len(argv) > 1 and _arg(argv, "--as"):
            user = conn.execute("SELECT username, role FROM users WHERE username = ? AND active = 1",
                                (_arg(argv, "--as"),)).fetchone()
            if user is None:
                print("no such active user")
                return 2
            batch = stage(conn, argv[1], user["username"], user["role"], clock_offset=offset)
            print(json.dumps({"batch": batch, **_totals([(r["state"], r["kind"]) for r in items(conn, batch)])},
                             indent=2))
            return 0
        if argv[:1] == ["--reconcile"]:
            print(json.dumps(reconcile(conn, apply="--apply" in argv), indent=2))
            return 0
    except ImportProblem as e:
        print(str(e))
        return 1
    finally:
        conn.close()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
