"""Patient documents and their retrieval (P15). Sources, never answers.

WHAT COMES IN. PDF, PNG, JPEG and plain UTF-8 text, decided by the file's own
bytes. A name that disagrees with the bytes, SVG, HTML, archives, executables,
active PDF content (JavaScript, launch actions, embedded files, XFA forms) and
password-protected PDFs are quarantined: kept, never read further. Size, page
count and pixel count are checked before anything is decoded - an image's
size comes from its header, so a decompression bomb is refused unopened.

HOW IT IS READ. In a separate process (document_worker.py) started with the
network denied by the operating system, a CPU limit, a file-size limit, a
stripped environment and its own empty temporary folder. OCR is Tesseract with
pinned language data. Nothing in a document is executed or followed; its text
is data. macOS does not enforce memory rlimits, so worker_guard measures the
worker's whole process tree from outside and kills it past MEMORY_LIMIT. At
most WORKER_SLOTS workers run at once, across processes, so all of them
together stay under WORKER_SLOTS * (MEMORY_LIMIT + worker_guard.TOLERANCE).

WHEN IT COUNTS. Extracted text is not part of the record until a dentist
confirms it. Only then is it indexed. Unconfirmed, rejected, quarantined,
failed and superseded documents are never searched and never exported.

WHO SEES WHAT. Dentist only (read_clinical). The index is filtered by patient,
and every hit is checked again against this database before it is shown - the
index filter is never the only control. A replacement supersedes; nothing is
overwritten. Originals are stored per patient under DOC_ROOT, mode 0600.
"""
import hashlib
import json
import os
import re
import fcntl
import resource
import shutil
import sys
import tempfile
import time
from pathlib import Path

import clinic_time
import worker_guard
from auth import authorize, log_audit

ROOT = Path(__file__).resolve().parent
CAPABILITY = "read_clinical"
DOC_ROOT = Path("documents")
DOC_CHROMA_PATH = "db/doc_chroma"
TESSDATA = ROOT / "models" / "ocr" / "tessdata"
EXTRACTOR = "p15.1 pypdf-6.19.0 tesseract-5.5.3 tessdata_fast@87416418"

MAX_BYTES = 10 * 1024 * 1024
MAX_PAGES = 50
MAX_PIXELS = 25_000_000
MAX_SIDE = 10_000
MAX_TEXT_BYTES = 1024 * 1024
WORKER_TIMEOUT = 60
# a 25-megapixel image (MAX_PIXELS) peaks at ~510 MB under OCR, measured 2026-09-23
MEMORY_LIMIT = 768 * 1024 * 1024
WORKER_SLOTS = 2
SLOT_WAIT = 30
SLOT_DIR = Path("db") / "document-worker-slots"
UNCERTAIN_BELOW = 60
CHUNK = 1000
TOP_K = 10

STATUSES = ("quarantined", "extraction_failed", "pending_review", "confirmed", "rejected",
            "superseded")
EXTENSIONS = {"pdf": (".pdf",), "png": (".png",), "jpeg": (".jpg", ".jpeg"), "text": (".txt",)}
MIMETYPES = {"pdf": "application/pdf", "png": "image/png", "jpeg": "image/jpeg",
             "text": "text/plain; charset=utf-8"}
# a name must end where a PDF name ends, or "/AAAAAB+Helvetica" (an ordinary
# subset font) would read as an additional-actions key
ACTIVE_PDF = re.compile(rb"/(JavaScript|JS|Launch|EmbeddedFiles?|RichMedia|XFA|AA)(?=[\s<>\[\]/()])")

SCHEMA = """
    CREATE TABLE IF NOT EXISTS patient_documents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT NOT NULL,
        kind TEXT,
        display_name TEXT NOT NULL,
        stored_path TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        size INTEGER NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('quarantined', 'extraction_failed',
            'pending_review', 'confirmed', 'rejected', 'superseded')),
        reason TEXT,
        extraction TEXT,
        extractor TEXT,
        uploaded_by TEXT NOT NULL,
        uploaded_at TEXT NOT NULL,
        decided_by TEXT,
        decided_at TEXT,
        supersedes_id INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_patient_documents_patient
        ON patient_documents (patient_id, status);
    -- the same bytes for the same patient are one document, until rejected
    CREATE UNIQUE INDEX IF NOT EXISTS idx_patient_documents_once
        ON patient_documents (patient_id, sha256) WHERE status NOT IN ('rejected', 'superseded');
    CREATE TRIGGER IF NOT EXISTS patient_documents_extraction_fixed
        BEFORE UPDATE OF extraction ON patient_documents
        WHEN OLD.extraction IS NOT NULL AND NEW.extraction IS NOT OLD.extraction
        BEGIN SELECT RAISE(ABORT, 'an extraction is never rewritten'); END;
    CREATE TRIGGER IF NOT EXISTS patient_documents_sha_fixed
        BEFORE UPDATE OF sha256 ON patient_documents
        WHEN NEW.sha256 IS NOT OLD.sha256
        BEGIN SELECT RAISE(ABORT, 'an original is never rewritten'); END;
"""


class DocumentError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _now(now):
    return clinic_time.to_storage(now or clinic_time.now_utc())


def _require(conn, actor, role, action, target):
    if not authorize(role, CAPABILITY):
        log_audit(conn, actor, role, action, target, allowed=0)
        raise PermissionError(f"{role} may not work with clinical documents")


# --- what the bytes are ------------------------------------------------------

def detect(data, name):
    """-> 'pdf' | 'png' | 'jpeg' | 'text' | None, from the bytes alone."""
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data and b"\x00" not in data[:MAX_TEXT_BYTES] and len(data) <= MAX_TEXT_BYTES:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return None
        lowered = text.lstrip().lower()
        if lowered.startswith(("<", "<?xml", "<!doctype")):
            return None
        return "text"
    return None


def image_size(data, kind):
    """(width, height) from the header, without decoding a single pixel."""
    if kind == "png":
        if len(data) < 24 or data[12:16] != b"IHDR":
            return None
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
        i += 2 + length
    return None


def _refusal(data, name, kind):
    """A reason to quarantine, or None."""
    if not data:
        return "empty file"
    if len(data) > MAX_BYTES:
        return "too large"
    if kind is None:
        return "type not allowed"
    ext = Path(name or "").suffix.lower()
    if ext and ext not in EXTENSIONS[kind]:
        return "name does not match content"
    if kind in ("png", "jpeg"):
        size = image_size(data, kind)
        if size is None:
            return "unreadable image header"
        w, h = size
        if w <= 0 or h <= 0 or w > MAX_SIDE or h > MAX_SIDE or w * h > MAX_PIXELS:
            return "too many pixels"
    if kind == "pdf" and ACTIVE_PDF.search(data):
        return "active content"
    return None


def display_name(name):
    base = Path(str(name or "")).name
    base = re.sub(r"[\x00-\x1f\x7f/\\\\]", "", base).strip() or "document"
    return base[:120]


# --- the sandboxed worker ----------------------------------------------------

SANDBOX_PROFILE = "(version 1)(allow default)(deny network*)"


def sandbox_command(argv):
    """argv, run with every network operation denied by the operating system."""
    return ["/usr/bin/sandbox-exec", "-p", SANDBOX_PROFILE] + list(argv)


def _limits():
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024, 16 * 1024 * 1024))


def _slot():
    """An open, locked slot file, or None when every slot stays busy for SLOT_WAIT."""
    Path(SLOT_DIR).mkdir(parents=True, exist_ok=True)
    os.chmod(SLOT_DIR, 0o700)
    deadline = time.monotonic() + SLOT_WAIT
    while True:
        for i in range(WORKER_SLOTS):
            f = open(Path(SLOT_DIR) / f"slot-{i}", "a")
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return f
            except OSError:
                f.close()
        if time.monotonic() > deadline:
            return None
        time.sleep(0.05)


def _extract(path, kind):
    if not Path("/usr/bin/sandbox-exec").exists() or not worker_guard.available():
        # no sandbox or no memory guard, no extraction: fail closed
        return {"error": "sandbox_unavailable"}
    slot = _slot()
    if slot is None:
        return {"error": "busy"}
    work = tempfile.mkdtemp(prefix="docwork-")
    try:
        env = {"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "TESSDATA_PREFIX": str(TESSDATA),
               "HOME": work, "TMPDIR": work, "LANG": "C.UTF-8", "OMP_THREAD_LIMIT": "1",
               "PYTHONDONTWRITEBYTECODE": "1"}
        run = worker_guard.run(sandbox_command([sys.executable, str(ROOT / "document_worker.py"),
                                                kind, str(Path(path).resolve())]),
                               MEMORY_LIMIT, WORKER_TIMEOUT, env=env, cwd=work, preexec=_limits)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        slot.close()
    if run.outcome != "ok":
        return {"error": run.outcome, "peak": run.peak}
    try:
        return json.loads(run.stdout)
    except ValueError:
        return {"error": "crashed"}


# --- ingest and review ---------------------------------------------------------

def original_path(row):
    return Path(DOC_ROOT) / row["stored_path"]


def row(conn, doc_id):
    return conn.execute("SELECT * FROM patient_documents WHERE id = ?", (doc_id,)).fetchone()


def _store(pid, sha, data):
    """Write the original once, atomically. -> True if this call created it.

    Originals are kept per patient (DOC_ROOT/<patient>/<sha256>): the same bytes
    uploaded for two patients are two files, never one shared blob, so nothing
    about one patient's copy depends on the other's."""
    dest = Path(DOC_ROOT) / pid / sha
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(dest.parent, 0o700)
    if dest.exists():
        return False
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".upload-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.link(tmp, dest)
    except FileExistsError:
        return False
    finally:
        os.unlink(tmp)
    return True


def ingest(conn, pid, data, name, actor, role, now=None, supersedes=None):
    """-> document id. Stored, read in the sandbox, held for review or quarantined."""
    _require(conn, actor, role, "document_upload", f"patient:{pid}")
    sha = hashlib.sha256(data).hexdigest()
    kind = detect(data, name)
    refusal = _refusal(data, name, kind)
    rel = f"{pid}/{sha}"
    # one writer at a time: the duplicate check, the file and the row succeed or
    # fail together, and a file this call wrote never outlives a failed insert
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    created = False
    try:
        existing = conn.execute("SELECT id FROM patient_documents WHERE patient_id = ?"
                                " AND sha256 = ? AND status NOT IN ('rejected', 'superseded')",
                                (pid, sha)).fetchone()
        if existing:
            conn.rollback()
            return existing[0]
        created = _store(pid, sha, data)
        cur = conn.execute(
            "INSERT INTO patient_documents (patient_id, kind, display_name, stored_path, sha256,"
            " size, status, reason, uploaded_by, uploaded_at, supersedes_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (pid, kind, display_name(name), rel, sha, len(data),
             "quarantined" if refusal else "extraction_failed", refusal, actor, _now(now),
             supersedes))
        conn.commit()
    except BaseException:
        if created and not conn.execute("SELECT 1 FROM patient_documents WHERE patient_id = ?"
                                        " AND stored_path = ?", (pid, rel)).fetchone():
            (Path(DOC_ROOT) / rel).unlink(missing_ok=True)
        conn.rollback()
        raise
    doc_id = cur.lastrowid
    log_audit(conn, actor, role, "document_upload", f"document:{doc_id}", allowed=1,
              reason="quarantined" if refusal else "stored")
    if refusal:
        return doc_id

    _read(conn, doc_id, kind, rel, actor, role)
    return doc_id


# what staff see when reading fails; the code alone goes to the audit log
QUARANTINE = {"protected": "password protected", "too_many_pages": "too many pages",
              "active_content": "active content", "too_complex": "too complex to read safely"}
FAILURE = {"memory_limit": "too large to read safely", "timeout": "took too long to read",
           "busy": "the reader was busy - try again"}


def _read(conn, doc_id, kind, rel, actor, role):
    """Run the worker once. Only a complete result is written; a failure writes no text."""
    result = _extract(Path(DOC_ROOT) / rel, kind)
    code = result.get("error")
    log_audit(conn, actor, role, "document_extract", f"document:{doc_id}", allowed=1,
              reason=code or "ok")
    if code:
        quarantine = code in QUARANTINE
        conn.execute("UPDATE patient_documents SET status = ?, reason = ? WHERE id = ?"
                     " AND status = 'extraction_failed'",
                     ("quarantined" if quarantine else "extraction_failed",
                      QUARANTINE.get(code) or FAILURE.get(code, "could not be read"), doc_id))
        conn.commit()
        return
    pages = result["pages"]
    empty = not any(p["text"].strip() for p in pages)
    # only the first complete reading counts; a second one racing it changes nothing
    conn.execute("UPDATE patient_documents SET status = 'pending_review', extraction = ?,"
                 " extractor = ?, reason = ? WHERE id = ? AND status = 'extraction_failed'"
                 " AND extraction IS NULL",
                 (json.dumps({"pages": pages}), EXTRACTOR, "no text found" if empty else None,
                  doc_id))
    conn.commit()


def retry(conn, doc_id, pid, actor, role):
    """Read a document again after a failure (memory, time, busy, crash). Never a quarantine."""
    r = _own(conn, doc_id, pid, actor, role, "document_retry")
    claimed = conn.execute("UPDATE patient_documents SET reason = 'reading again' WHERE id = ?"
                           " AND status = 'extraction_failed'", (doc_id,)).rowcount
    conn.commit()
    if not claimed:
        raise DocumentError("not_failed", "only a document that could not be read can be retried")
    log_audit(conn, actor, role, "document_retry", f"document:{doc_id}", allowed=1)
    _read(conn, doc_id, r["kind"], r["stored_path"], actor, role)


def _own(conn, doc_id, pid, actor, role, action):
    _require(conn, actor, role, action, f"document:{doc_id}")
    r = conn.execute("SELECT * FROM patient_documents WHERE id = ? AND patient_id = ?",
                     (doc_id, pid)).fetchone()
    if r is None:
        log_audit(conn, actor, role, action, f"document:{doc_id}", allowed=0)
        raise LookupError("no such document for this patient")
    return r


def load(conn, doc_id, pid, actor, role):
    r = _own(conn, doc_id, pid, actor, role, "document_read")
    log_audit(conn, actor, role, "document_read", f"document:{doc_id}", allowed=1)
    return r


def confirm(conn, doc_id, pid, actor, role, now=None):
    """The dentist accepts the extracted text as part of the record. Indexed once."""
    _own(conn, doc_id, pid, actor, role, "document_confirm")
    claimed = conn.execute(
        "UPDATE patient_documents SET status = 'confirmed', decided_by = ?, decided_at = ?"
        " WHERE id = ? AND patient_id = ? AND status = 'pending_review'",
        (actor, _now(now), doc_id, pid)).rowcount
    conn.commit()
    if not claimed:
        raise DocumentError("not_pending", "this document is not waiting for review")
    try:
        _index(row(conn, doc_id))
    except Exception:
        conn.execute("UPDATE patient_documents SET status = 'pending_review', decided_by = NULL,"
                     " decided_at = NULL WHERE id = ?", (doc_id,))
        conn.commit()
        raise DocumentError("index_failed", "the document could not be indexed; it is still"
                                            " waiting") from None
    log_audit(conn, actor, role, "document_confirm", f"document:{doc_id}", allowed=1)


def reject(conn, doc_id, pid, reason, actor, role, now=None):
    _own(conn, doc_id, pid, actor, role, "document_reject")
    changed = conn.execute(
        "UPDATE patient_documents SET status = 'rejected', decided_by = ?, decided_at = ?,"
        " reason = ? WHERE id = ? AND status IN ('pending_review', 'quarantined',"
        " 'extraction_failed')", (actor, _now(now), (reason or "")[:300], doc_id)).rowcount
    conn.commit()
    if not changed:
        raise DocumentError("not_pending", "this document cannot be rejected now")
    unindex([doc_id])
    log_audit(conn, actor, role, "document_reject", f"document:{doc_id}", allowed=1)


def replace(conn, doc_id, pid, data, name, actor, role, now=None):
    """A new version. The old one is kept, marked superseded, and leaves the index."""
    old = _own(conn, doc_id, pid, actor, role, "document_replace")
    if old["status"] not in ("pending_review", "confirmed"):
        raise DocumentError("not_current", "only a current document can be replaced")
    conn.execute("UPDATE patient_documents SET status = 'superseded', decided_by = ?,"
                 " decided_at = ? WHERE id = ?", (actor, _now(now), doc_id))
    conn.commit()
    unindex([doc_id])
    new_id = ingest(conn, pid, data, name, actor, role, now=now, supersedes=doc_id)
    log_audit(conn, actor, role, "document_replace", f"document:{doc_id}", allowed=1,
              reason=f"document:{new_id}")
    return new_id


def for_patient(conn, pid, limit=100):
    return conn.execute("SELECT * FROM patient_documents WHERE patient_id = ?"
                        " ORDER BY id DESC LIMIT ?", (pid, limit)).fetchall()


# --- the index -------------------------------------------------------------------

_collection_cache = {}


def _embedder_ready():
    """The local embedding model must already be on disk; it is never fetched."""
    cache = Path.home() / ".cache" / "chroma" / "onnx_models" / "all-MiniLM-L6-v2"
    return cache.exists() and any(cache.rglob("*.onnx"))


def collection():
    if DOC_CHROMA_PATH not in _collection_cache:
        if not _embedder_ready():
            raise DocumentError("embedder_missing", "the local search model is not installed")
        import chromadb
        from chromadb.config import Settings
        client = chromadb.PersistentClient(path=DOC_CHROMA_PATH,
                                           settings=Settings(anonymized_telemetry=False))
        _collection_cache[DOC_CHROMA_PATH] = client.get_or_create_collection("patient_documents")
    return _collection_cache[DOC_CHROMA_PATH]


def _chunks(text):
    text = text.strip()
    return [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)] if text else []


def _index(r):
    pages = json.loads(r["extraction"] or '{"pages": []}')["pages"]
    ids, texts, metas = [], [], []
    for p in pages:
        for n, chunk in enumerate(_chunks(p["text"])):
            ids.append(f"doc{r['id']}-p{p['page']}-c{n}")
            texts.append(chunk)
            metas.append({"patient_id": r["patient_id"], "doc_id": r["id"], "page": p["page"]})
    if ids:
        collection().upsert(ids=ids, documents=texts, metadatas=metas)


def unindex(doc_ids):
    col = collection()
    for doc_id in doc_ids:
        col.delete(where={"doc_id": doc_id})


def unindex_patient(pid):
    collection().delete(where={"patient_id": pid})


def reindex_patient(conn, pid):
    """After a merge the rows belong to the survivor; the index follows."""
    rows = conn.execute("SELECT * FROM patient_documents WHERE patient_id = ?"
                        " AND status = 'confirmed'", (pid,)).fetchall()
    unindex([r["id"] for r in rows])
    for r in rows:
        _index(r)


def rebuild_index(conn):
    """From the confirmed documents alone, e.g. after a restore."""
    col = collection()
    existing = col.get()["ids"]
    if existing:
        col.delete(ids=existing)
    for r in conn.execute("SELECT * FROM patient_documents WHERE status = 'confirmed'"):
        _index(r)


WORD = re.compile(r"[a-zà-ù0-9]{3,}")


def search(conn, pid, query, actor, role, k=TOP_K):
    """-> results for this patient only, each with its provenance. [] is honest."""
    _require(conn, actor, role, "document_search", f"patient:{pid}")
    log_audit(conn, actor, role, "document_search", f"patient:{pid}", allowed=1)
    terms = set(WORD.findall((query or "").lower()))
    if not terms:
        return []
    col = collection()
    count = col.count()
    if not count:
        return []
    res = col.query(query_texts=[query[:500]], n_results=min(count, 30),
                    where={"patient_id": pid})
    out, seen = [], set()
    for chunk, meta in zip(res["documents"][0], res["metadatas"][0]):
        # an honest "nothing found": a hit must share a word with the query,
        # or the nearest-neighbour search would always return something
        if not terms & set(WORD.findall(chunk.lower())):
            continue
        key = (meta["doc_id"], meta["page"])
        if key in seen:
            continue
        # the second control: this database, not the index, says whose it is
        r = conn.execute("SELECT * FROM patient_documents WHERE id = ? AND patient_id = ?"
                         " AND status = 'confirmed'", (meta["doc_id"], pid)).fetchone()
        if r is None:
            continue
        seen.add(key)
        page = next((p for p in json.loads(r["extraction"])["pages"]
                     if p["page"] == meta["page"]), {"confidence": 0})
        out.append({"doc_id": r["id"], "page": meta["page"], "snippet": chunk[:300],
                    "display_name": r["display_name"], "kind": r["kind"], "sha256": r["sha256"],
                    "extractor": r["extractor"], "uploaded_by": r["uploaded_by"],
                    "uploaded_at": r["uploaded_at"], "confirmed_by": r["decided_by"],
                    "confirmed_at": r["decided_at"], "confidence": page["confidence"],
                    "uncertain": page["confidence"] < UNCERTAIN_BELOW})
        if len(out) >= k:
            break
    return out


def exportable(conn, pid):
    """(archive name, bytes) of this patient's confirmed originals. No extracted
    text, no unreviewed or refused file, no internal metadata."""
    out = []
    for r in conn.execute("SELECT * FROM patient_documents WHERE patient_id = ?"
                          " AND status = 'confirmed' ORDER BY id", (pid,)):
        path = original_path(r)
        if path.exists():
            out.append((f"documents/{r['id']}-{r['display_name']}", path.read_bytes()))
    return out
