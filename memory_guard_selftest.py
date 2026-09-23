"""The document worker's memory budget (P15 follow-up): is it really enforced?

macOS does not enforce memory rlimits, so worker_guard watches the worker's
process tree from the parent. These checks drive it with workers built to blow
the budget - one huge allocation, steady growth, a child process, a child that
leaves the process group, a decompression bomb, a deep page tree, OCR of a
maximum-size image under a lowered budget - and require that the tree is killed
before it passes MEMORY_LIMIT + TOLERANCE, that nothing is left behind, and
that a later retry works. Where sips, tesseract or sandbox-exec is missing the
checks that need them are reported SKIPPED, never passed.
"""
import io
import resource
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from pathlib import Path

import documents as docs
import patient_id
import worker_guard as guard
from documents_selftest import D, pdf, png_header, setup

MB = 1024 * 1024
LIMIT = 256 * MB


def alive(marker):
    return subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout.split()


def within(r, limit=LIMIT):
    return r.peak <= limit + guard.TOLERANCE and r.maxrss <= limit + guard.TOLERANCE


def build(objs):
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offs = []
    for i, body in enumerate(objs, 1):
        offs.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + body + b"\nendobj\n")
    x = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for o in offs:
        out.write(f"{o:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF\n".encode())
    return out.getvalue()


def flate_bomb(expanded):
    """A one-page PDF whose content stream is small on disk and `expanded` bytes inflated."""
    z = zlib.compress(b"BT /F1 12 Tf 10 10 Td (bomba) Tj ET\n" + b" " * expanded, 9)
    return build([b"<< /Type /Catalog /Pages 2 0 R >>",
                  b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                  b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
                  b" /Resources << /Font << /F1 5 0 R >> >> >>",
                  b"<< /Length " + str(len(z)).encode() + b" /Filter /FlateDecode >>\nstream\n"
                  + z + b"\nendstream",
                  b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"])


def deep_tree(depth):
    """One page under `depth` nested /Pages nodes."""
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    for i in range(depth):
        parent = f"/Parent {1 + i} 0 R" if i else ""
        objs.append(f"<< /Type /Pages /Kids [{3 + i} 0 R] /Count 1 {parent} >>".encode())
    objs.append(f"<< /Type /Page /Parent {1 + depth} 0 R /MediaBox [0 0 612 792] >>".encode())
    return build(objs)


def rlimit_evidence():
    # M0. record what macOS does with memory rlimits (evidence, not a pass condition)
    found = {}
    for name in ("RLIMIT_AS", "RLIMIT_DATA", "RLIMIT_RSS"):
        lim = getattr(resource, name)
        try:
            resource.setrlimit(lim, (LIMIT, resource.getrlimit(lim)[1]))
            found[name] = "accepted"
            resource.setrlimit(lim, (resource.RLIM_INFINITY, resource.getrlimit(lim)[1]))
        except (ValueError, OSError) as e:
            found[name] = f"refused ({type(e).__name__})"
    print("M0 macOS memory rlimits:", found)


def guard_alone():
    # M1. one huge allocation: killed before it passes the limit + tolerance
    r = guard.run([sys.executable, "-c", "x = b'x' * (2 << 30)  # MARK_m1"], LIMIT, 30)
    assert r.outcome == "memory_limit" and r.returncode == -9, r
    assert within(r), f"M1: past the tolerance: peak {r.peak // MB} MB, maxrss {r.maxrss // MB} MB"
    assert not alive("MARK_m1"), "M1: worker still alive"

    # M2. steady growth in 64 MB steps
    r = guard.run([sys.executable, "-c", "l = []  # MARK_m2\nwhile True: l.append(b'y' * (64 << 20))"],
                  LIMIT, 30)
    assert r.outcome == "memory_limit" and within(r), r

    # M3. the memory is in a child process: the tree is measured, not just the worker
    child = "x = b'z' * (2 << 30)  # MARK_m3c"
    r = guard.run([sys.executable, "-c", "import subprocess, sys  # MARK_m3\n"
                   f"subprocess.run([sys.executable, '-c', {child!r}])"], LIMIT, 30)
    assert r.outcome == "memory_limit" and r.processes >= 2 and r.peak <= LIMIT + guard.TOLERANCE, r
    assert not alive("MARK_m3"), "M3: the worker or its child survived"

    # M4. a child that leaves the process group (setsid) is still seen and killed
    code = ("import os, time  # MARK_m4\n"
            "if os.fork() == 0:\n    os.setsid()\n    x = b'q' * (2 << 30)\n    time.sleep(30)\n"
            "time.sleep(30)")
    r = guard.run([sys.executable, "-c", code], LIMIT, 30)
    assert r.outcome == "memory_limit" and r.peak <= LIMIT + guard.TOLERANCE, r
    time.sleep(0.2)
    assert not alive("MARK_m4"), "M4: an escaped child survived"

    # M5. an ordinary worker: output intact, small; its leftover children are killed
    r = guard.run([sys.executable, "-c", "import subprocess, sys  # MARK_m5\n"
                   "subprocess.Popen([sys.executable, '-c', 'import time  # MARK_m5c\\ntime.sleep(30)'])\n"
                   "print('fine')"], LIMIT, 30)
    assert r.outcome == "ok" and r.stdout == "fine\n" and r.peak < LIMIT, r
    time.sleep(0.2)
    assert not alive("MARK_m5c"), "M5: a finished worker left a child running"

    # M6. time and output are bounded too
    r = guard.run([sys.executable, "-c", "import time  # MARK_m6\ntime.sleep(30)"], LIMIT, 0.3)
    assert r.outcome == "timeout" and not alive("MARK_m6"), r
    r = guard.run([sys.executable, "-c", "import sys\nwhile True: sys.stdout.write('a' * 65536)"],
                  LIMIT, 30)
    assert r.outcome == "too_much_output" and r.stdout == "", r.outcome
    print(f"M1-M6 guard: killed at {LIMIT // MB} MB (+{guard.TOLERANCE // MB} MB tolerance)")


def leftovers(before):
    work = {p.name for p in Path(tempfile.gettempdir()).glob("docwork-*")}
    return work - before


def documents_under_budget(tmp):
    have_ocr = shutil.which("sips") and shutil.which("tesseract") and shutil.which("sandbox-exec")
    conn = setup(tmp)
    docs.SLOT_DIR = Path(tmp) / "slots"
    pid = patient_id.seed_patient(conn, "ZZPM000000000001", "Mara Memoria")
    other = patient_id.seed_patient(conn, "ZZPM000000000002", "Nico Memoria")
    before = {p.name for p in Path(tempfile.gettempdir()).glob("docwork-*")}

    # M7. a small PDF that inflates to 60 MB: pypdf's own limit (20 MB, set
    # explicitly) stops it, the document is quarantined, nothing is read
    bomb = flate_bomb(60 * MB)
    assert len(bomb) < 200 * 1024
    r = docs.row(conn, docs.ingest(conn, pid, bomb, "bomba.pdf", *D))
    assert r["status"] == "quarantined" and r["reason"] == "too complex to read safely", dict(r)
    assert r["extraction"] is None

    # M8. a page tree 200 levels deep
    r = docs.row(conn, docs.ingest(conn, pid, deep_tree(200), "albero.pdf", *D))
    assert r["status"] == "quarantined" and r["reason"] == "too complex to read safely", dict(r)

    # M9. oversized decoded image data: refused from the header, the worker never runs
    calls = []
    real = guard.run
    guard.run = lambda *a, **k: calls.append(a) or real(*a, **k)
    try:
        big = png_header(9000, 9000)
        jpeg = (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
                b"\xff\xc0\x00\x11\x08" + (9000).to_bytes(2, "big") + (9000).to_bytes(2, "big")
                + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01" + b"\x00" * 64)
        for data, name in ((big, "huge.png"), (jpeg, "huge.jpg")):
            r = docs.row(conn, docs.ingest(conn, pid, data, name, *D))
            assert r["status"] == "quarantined", (name, dict(r))
    finally:
        guard.run = real
    assert not calls, "M9: an oversized image reached the worker"

    if not have_ocr:
        print("SKIPPED M10-M12: sips, tesseract or sandbox-exec missing")
        conn.close()
        return
    src = Path(tmp) / "scan.pdf"
    src.write_bytes(pdf(["Referto: otturazione dente 36, controllo tra sei mesi"]))
    small = Path(tmp) / "scan.png"
    large = Path(tmp) / "large.png"
    subprocess.run(["sips", "-s", "format", "png", str(src), "--out", str(small)],
                   check=True, capture_output=True)
    subprocess.run(["sips", "-z", "5000", "5000", str(small), "--out", str(large)],
                   check=True, capture_output=True)

    # M10. OCR past the budget: tesseract (a child of the worker) is what grows.
    # the tree is killed, nothing is written, nothing indexed, nothing left over
    saved = docs.MEMORY_LIMIT
    docs.MEMORY_LIMIT = 150 * MB
    seen = []
    guard.run = lambda *a, **k: seen.append(real(*a, **k)) or seen[-1]
    try:
        did = docs.ingest(conn, pid, large.read_bytes(), "grande.png", *D)
    finally:
        docs.MEMORY_LIMIT = saved
        guard.run = real
    r = docs.row(conn, did)
    assert r["status"] == "extraction_failed" and r["reason"] == "too large to read safely", dict(r)
    assert r["extraction"] is None and r["extractor"] is None
    assert seen[0].outcome == "memory_limit" and seen[0].processes >= 2, seen[0]
    assert seen[0].peak <= 150 * MB + guard.TOLERANCE, f"M10: peak {seen[0].peak // MB} MB"
    assert docs.original_path(r).exists(), "M10: the original must stay for a person to see"
    audit = conn.execute("SELECT reason FROM audit_log WHERE action = 'document_extract'"
                         " AND target = ?", (f"document:{did}",)).fetchall()
    assert [a[0] for a in audit] == ["memory_limit"], audit
    assert not docs.search(conn, pid, "otturazione", *D)
    time.sleep(0.2)
    assert not alive(str(large)) and not alive("document_worker.py"), "M10: a worker survived"
    assert not leftovers(before), f"M10: temporary folders left: {leftovers(before)}"
    held = [docs._slot() for _ in range(docs.WORKER_SLOTS)]
    assert all(held), "M10: a slot was not released"
    [h.close() for h in held]

    # M11. retry after the failure: with the normal budget the same original is
    # read; a second retry is refused; another patient's URL cannot retry it
    try:
        docs.retry(conn, did, other, *D)
        raise AssertionError("M11: retried through another patient")
    except LookupError:
        pass
    docs.retry(conn, did, pid, *D)
    r = docs.row(conn, did)
    assert r["status"] == "pending_review" and "otturazione" in r["extraction"].lower(), dict(r)
    try:
        docs.retry(conn, did, pid, *D)
        raise AssertionError("M11: retried a document that was read")
    except docs.DocumentError:
        pass

    # M12. concurrent workers: never more than WORKER_SLOTS at once, so the
    # total stays under WORKER_SLOTS * (MEMORY_LIMIT + TOLERANCE); a request that
    # finds every slot busy fails cleanly and can be retried
    held = [docs._slot() for _ in range(docs.WORKER_SLOTS)]
    docs.SLOT_WAIT = 0.3
    try:
        busy = docs.ingest(conn, pid, small.read_bytes(), "attesa.png", *D)
    finally:
        [h.close() for h in held]
        docs.SLOT_WAIT = 30
    assert docs.row(conn, busy)["reason"] == "the reader was busy - try again"
    docs.retry(conn, busy, pid, *D)
    assert docs.row(conn, busy)["status"] == "pending_review"

    running, most, peaks = [0], [0], []
    lock = threading.Lock()

    def counted(*a, **k):
        with lock:
            running[0] += 1
            most[0] = max(most[0], running[0])
        try:
            res = real(*a, **k)
            peaks.append(res.peak)
            return res
        finally:
            with lock:
                running[0] -= 1

    guard.run = counted
    results = []

    def one(i):
        c = setup_conn(tmp)
        img = large.read_bytes() + bytes([i])  # distinct files, same size
        results.append(docs.row(c, docs.ingest(c, pid, img, f"c{i}.png", *D))["status"])
        c.close()
    try:
        ts = [threading.Thread(target=one, args=(i,)) for i in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
    finally:
        guard.run = real
    assert most[0] <= docs.WORKER_SLOTS, f"M12: {most[0]} workers at once"
    assert sorted(results) == ["pending_review"] * 4, results
    assert max(peaks) <= docs.MEMORY_LIMIT + guard.TOLERANCE, peaks
    assert not leftovers(before)
    print(f"M7-M12 documents: bomb and deep tree quarantined, OCR killed at {seen[0].peak // MB} MB"
          f" under a 150 MB budget, retry ok, {most[0]} workers at once, peaks"
          f" {[p // MB for p in peaks]} MB")

    # M13. no guard, no reading: fail closed
    saved = guard.available
    guard.available = lambda: False
    try:
        r = docs.row(conn, docs.ingest(conn, other, b"testo senza guardia", "g.txt", *D))
    finally:
        guard.available = saved
    assert r["status"] == "extraction_failed" and r["extraction"] is None, dict(r)
    conn.close()


def setup_conn(tmp):
    from storage import init_db
    return init_db(str(Path(tmp) / "d.sqlite"))


def budget():
    # M14. the documented budget: one worker tree under 1 GiB including the
    # tolerance, at most two at once, so documents never hold more than 2 GiB
    assert docs.MEMORY_LIMIT + guard.TOLERANCE <= 1024 * MB, docs.MEMORY_LIMIT
    assert docs.WORKER_SLOTS <= 2 and guard.POLL <= 0.01, (docs.WORKER_SLOTS, guard.POLL)


def selftest():
    budget()
    rlimit_evidence()
    if not guard.available():
        print("SKIPPED: libproc missing, the guard cannot run - documents refuse to read")
        return
    guard_alone()
    with tempfile.TemporaryDirectory() as tmp:
        documents_under_budget(tmp)
    print("selftest ok")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
    else:
        print("usage: python memory_guard_selftest.py --selftest")
        sys.exit(1)
