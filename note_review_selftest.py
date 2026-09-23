"""Uploaded-note review (POL-9): an upload never becomes a visit on its own.

Drives the real storage sync path with a synthetic routed note, the way the
upload worker and the watcher hand one over, then the dentist's review. A
throwaway database, sorted/ and staging/ per run; no model, no network.
"""
import hashlib
import json
import re
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

import clinic_time
import note_review as nr
import patient_id
import storage
from auth import authorize
from dental_notes_schema import DentalNote

ROOT = Path(__file__).resolve().parent
D, A, ADM = ("drossi", "dentist"), ("aassist", "assistant"), ("anadmin", "admin")
CF, OTHER = "ZZUA000000000001", "ZZUB000000000002"


def routed(sorted_root, cf, name, procedures=("rct 26",), notes="rct 26 prima seduta",
           visit_date="2026-05-05", raw="rct 26 1a seduta"):
    """What sort_files leaves behind for a readable note: the original and its
    extraction side by side under sorted/<cf>/notes/."""
    folder = Path(sorted_root) / cf / "notes"
    folder.mkdir(parents=True, exist_ok=True)
    txt = folder / f"{name}.txt"
    txt.write_text(raw)
    note = DentalNote(patient_name="Ugo Upload", codice_fiscale=cf, visit_date=visit_date,
                      procedures=list(procedures), clinical_notes=notes)
    js = txt.with_suffix(".json")
    js.write_text(note.model_dump_json())
    return txt, js


def audit_count(conn, action, allowed=1):
    return conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = ? AND allowed = ?",
                        (action, allowed)).fetchone()[0]


def visits_of(conn, pid):
    return conn.execute("SELECT * FROM visits WHERE patient_id = ? ORDER BY id", (pid,)).fetchall()


def domain(tmp):
    tmp = Path(tmp)
    sorted_root, staging = tmp / "sorted", tmp / "staging"
    nr.STAGING_ROOT = staging
    db = str(tmp / "clinic.sqlite")
    conn = storage.init_db(db)
    collection = storage.get_collection(str(tmp / "chroma"))
    t0 = clinic_time.read_instant("2026-09-23T08:00:00+00:00")
    pid = patient_id.seed_patient(conn, CF, "Ugo Upload")
    other = patient_id.seed_patient(conn, OTHER, "Olga Other")

    # 1. only a dentist reviews uploads
    assert authorize("dentist", nr.CAPABILITY)
    assert not authorize("assistant", nr.CAPABILITY) and not authorize("admin", nr.CAPABILITY)

    # 2. an upload handed to the sync path is STAGED: no visit, nothing
    # searchable, files moved out of sorted/ into staging/
    txt, js = routed(sorted_root, CF, "u1")
    original_bytes, extraction_text = txt.read_bytes(), js.read_text()
    out = storage.sync_note_file(js, sorted_root, conn, collection, "assistant", "aassist",
                                 target=str(txt))
    assert out == "staged", out
    assert visits_of(conn, pid) == [], "2: AN UPLOAD WAS FILED WITHOUT REVIEW"
    assert not txt.exists() and not js.exists(), "2: staged files left in sorted/"
    assert not collection.get(where={"codice_fiscale": CF})["ids"], "2: an upload became searchable"
    row = conn.execute("SELECT * FROM note_reviews WHERE origin = 'upload'").fetchone()
    assert row["status"] == "pending" and row["patient_id"] == pid and row["codice_fiscale"] == CF
    assert row["extraction"] == extraction_text, "2: extraction not kept as returned"
    assert row["original_sha256"] == hashlib.sha256(original_bytes).hexdigest()
    staged = nr.staged_files(row)
    assert staged["original"].read_bytes() == original_bytes, "2: original not preserved"
    assert audit_count(conn, "note_staged") == 1
    u1 = row["id"]

    # 3. the other roads into visits are gated the same way: backfill and
    # load_from_sorted stage a leftover routed note instead of filing it
    _txt2, js2 = routed(sorted_root, CF, "u2", procedures=("filling 14",), notes="otturazione 14",
                        visit_date="2026-06-06", raw="ott 14")
    landed, already, failed = storage.backfill_sorted(sorted_root, conn, collection)
    assert landed == 0 and not failed, (landed, already, failed)
    assert visits_of(conn, pid) == [], "3: BACKFILL FILED AN UNREVIEWED NOTE"
    _txt3, js3 = routed(sorted_root, CF, "u3", raw="nota tre")
    storage.load_from_sorted(sorted_root, conn, collection, "dentist", "drossi")
    assert visits_of(conn, pid) == [], "3: LOAD_FROM_SORTED FILED AN UNREVIEWED NOTE"
    assert conn.execute("SELECT COUNT(*) FROM note_reviews WHERE status = 'pending'"
                        " AND origin = 'upload'").fetchone()[0] == 3
    u2, u3 = [r[0] for r in conn.execute("SELECT id FROM note_reviews WHERE origin = 'upload'"
                                         " AND id != ? ORDER BY id", (u1,))]

    # 4. a typed note keeps its confirm path and is recorded as reviewed
    typed = DentalNote(patient_name="Ugo Upload", codice_fiscale=CF, visit_date="2026-04-04",
                       procedures=["prophy"], clinical_notes="pulizia")
    assert storage.save_new_note(typed, conn, collection, "dentist", "drossi",
                                 sorted_root=sorted_root) == "landed"
    typed_visit = visits_of(conn, pid)[0]
    rv = conn.execute("SELECT * FROM visit_reviews WHERE visit_id = ?", (typed_visit["id"],)).fetchone()
    assert rv and rv["method"] == "typed" and rv["reviewed_by"] == "drossi", rv

    # 5. assistant and admin cannot confirm, reject or retry; refusals audited
    for who in (A, ADM):
        for fn, args in ((nr.confirm, (u1, {})), (nr.reject, (u1, "r")), (nr.retry, (u1,))):
            try:
                fn(conn, *args, *who, sorted_root=sorted_root, collection=collection, now=t0)
                raise AssertionError(f"5: {who[1]} ran {fn.__name__}")
            except PermissionError:
                pass
    assert audit_count(conn, "note_review_confirm", 0) == 2

    # 6. the extraction and the original's checksum cannot be rewritten
    for sql in ("UPDATE note_reviews SET extraction = '{}' WHERE id = ?",
                "UPDATE note_reviews SET original_sha256 = 'x' WHERE id = ?"):
        try:
            conn.execute(sql, (u1,))
            raise AssertionError(f"6: rewrote {sql}")
        except sqlite3.DatabaseError:
            conn.rollback()

    # 7. confirm with an edit. the visit carries the dentist's fields, the
    # extraction and the staged files are untouched, both actions audited.
    # a codice fiscale in the form is ignored: the patient is the upload's.
    fields = {"visit_date": "2026-05-05", "procedures": "rct 26", "clinical_notes":
              "rct 26 prima seduta, rivedere tra 2 settimane", "next_appointment": "14d",
              "codice_fiscale": OTHER, "patient_name": "Olga Other"}
    vid = nr.confirm(conn, u1, fields, *D, sorted_root=sorted_root, collection=collection, now=t0)
    visit = conn.execute("SELECT * FROM visits WHERE id = ?", (vid,)).fetchone()
    assert visit["patient_id"] == pid, "7: A FORM FIELD MOVED THE NOTE TO ANOTHER PATIENT"
    assert visit["clinical_notes"].endswith("2 settimane"), visit["clinical_notes"]
    assert visits_of(conn, other) == []
    row = conn.execute("SELECT * FROM note_reviews WHERE id = ?", (u1,)).fetchone()
    assert row["status"] == "confirmed" and row["visit_id"] == vid and row["edited"] == 1
    assert row["extraction"] == extraction_text, "7: extraction overwritten"
    assert nr.staged_files(row)["original"].read_bytes() == original_bytes
    assert conn.execute("SELECT method FROM visit_reviews WHERE visit_id = ?",
                        (vid,)).fetchone()[0] == "upload_confirmed"
    assert audit_count(conn, "note_review_confirm") == 1 and audit_count(conn, "note_review_edit") == 1
    assert collection.get(where={"codice_fiscale": CF})["ids"], "7: a confirmed note is searchable"

    # 7b. the search index fails while a confirmed note is filed. the dentist's
    # decision stands and the visit is marked reviewed; the note is simply not
    # searchable yet, the same state a typed note is in when its index write
    # fails, and backfill repairs it. nothing is left half-reviewed.
    class BrokenIndex:
        def upsert(self, **_k):
            raise RuntimeError("index down")

        def get(self, **_k):
            return {"ids": []}

    _t, jsb = routed(sorted_root, CF, "ub", visit_date="2026-05-06", raw="rct 26 ancora")
    storage.sync_note_file(jsb, sorted_root, conn, collection, "assistant", "aassist")
    ub = conn.execute("SELECT MAX(id) FROM note_reviews").fetchone()[0]
    vb = nr.confirm(conn, ub, {}, *D, sorted_root=sorted_root, collection=BrokenIndex(), now=t0)
    assert conn.execute("SELECT status FROM note_reviews WHERE id = ?", (ub,)).fetchone()[0] \
        == "confirmed"
    assert conn.execute("SELECT method FROM visit_reviews WHERE visit_id = ?",
                        (vb,)).fetchone()[0] == "upload_confirmed", "7b: filed but not marked"
    assert audit_count(conn, "sync_note", 0) >= 1, "7b: the index failure is not recorded"
    # and a confirm whose record write itself fails goes back to pending
    _t, jsc = routed(sorted_root, CF, "uc", raw="nota c")
    storage.sync_note_file(jsc, sorted_root, conn, collection, "assistant", "aassist")
    uc = conn.execute("SELECT MAX(id) FROM note_reviews").fetchone()[0]
    try:
        nr.confirm(conn, uc, {"visit_date": "not a date"}, *D, sorted_root=sorted_root,
                   collection=collection, now=t0)
        raise AssertionError("7b: an invalid confirm succeeded")
    except nr.ReviewError as e:
        assert e.code == "invalid", e
    assert conn.execute("SELECT status FROM note_reviews WHERE id = ?", (uc,)).fetchone()[0] \
        == "pending", "7b: a refused confirm lost its place in the queue"
    nr.reject(conn, uc, "test", *D, now=t0)

    # 8. confirming again does nothing, and neither do two confirmations at once
    try:
        nr.confirm(conn, u1, fields, *D, sorted_root=sorted_root, collection=collection, now=t0)
        raise AssertionError("8: confirmed twice")
    except nr.ReviewError as e:
        assert e.code == "not_pending", e
    before = len(visits_of(conn, pid))
    results, lock, gate = [], threading.Lock(), threading.Barrier(2)

    def race():
        own = sqlite3.connect(db, timeout=15)
        own.row_factory = sqlite3.Row
        gate.wait()
        try:
            got = nr.confirm(own, u2, {}, *D, sorted_root=sorted_root, collection=collection,
                             now=t0)
        except nr.ReviewError as e:
            got = e.code
        except Exception as e:
            got = f"error:{type(e).__name__}:{e}"
        finally:
            own.close()
        with lock:
            results.append(got)

    threads = [threading.Thread(target=race) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(visits_of(conn, pid)) == before + 1, f"8: TWO CONFIRMS MADE TWO VISITS: {results}"
    assert sorted(map(str, results))[-1] == "not_pending", results

    # 9. reject keeps everything and files nothing
    nr.reject(conn, u3, "duplicate of u1", *D, sorted_root=sorted_root, collection=collection,
              now=t0)
    row = conn.execute("SELECT * FROM note_reviews WHERE id = ?", (u3,)).fetchone()
    assert row["status"] == "rejected" and row["decision_reason"] == "duplicate of u1"
    assert nr.staged_files(row)["original"].exists(), "9: a rejected upload lost its original"
    assert row["visit_id"] is None and len(visits_of(conn, pid)) == before + 1

    # 10. an unreadable note: extraction_failed, retry audited and counted.
    # a retry that fails stays failed; one that succeeds becomes pending, never
    # filed; a retry of something already extracted is refused
    bad = sorted_root / "needs_review" / "scan.txt"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("illeggibile")
    fid = nr.record_failed(conn, bad, "extract_failed", "aassist", "assistant", now=t0)
    row = conn.execute("SELECT * FROM note_reviews WHERE id = ?", (fid,)).fetchone()
    assert row["status"] == "extraction_failed" and row["extraction"] is None

    def still_bad(_text):
        raise ValueError("unreadable")

    try:
        nr.retry(conn, fid, *D, extract=still_bad, sorted_root=sorted_root, now=t0)
        raise AssertionError("10: a failed retry reported success")
    except nr.ReviewError as e:
        assert e.code == "extraction_failed", e
    good = DentalNote(patient_name="Ugo Upload", codice_fiscale=CF, visit_date="2026-07-07",
                      procedures=["x-ray 16"], clinical_notes="rx 16")
    nr.retry(conn, fid, *D, extract=lambda _t: good, sorted_root=sorted_root, now=t0)
    row = conn.execute("SELECT * FROM note_reviews WHERE id = ?", (fid,)).fetchone()
    assert row["status"] == "pending" and row["attempts"] == 2 and row["patient_id"] == pid
    assert json.loads(row["extraction"])["procedures"] == ["x-ray 16"]
    assert audit_count(conn, "note_review_retry") == 2
    assert not [v for v in visits_of(conn, pid) if v["visit_date"] == "2026-07-07"], \
        "10: a retried extraction was filed"
    try:
        nr.retry(conn, fid, *D, extract=lambda _t: good, sorted_root=sorted_root, now=t0)
        raise AssertionError("10: retried something already extracted")
    except nr.ReviewError as e:
        assert e.code == "not_failed", e

    # 11. legacy visits: classified once, never rewritten. a web-*.json visit
    # came through the typed confirm path; anything else waits for a dentist
    legacy_paths = (f"{CF}/notes/web-2026-01-01T000000.json", f"{CF}/notes/scan_old.json",
                    "erased:999")
    for n, path in enumerate(legacy_paths):
        conn.execute("INSERT INTO visits (patient_id, visit_date, procedures, clinical_notes,"
                     " source_path) VALUES (?, ?, '[\"prophy\"]', 'vecchia nota', ?)",
                     (pid, f"2025-0{n + 1}-01", path))
    conn.commit()
    snapshot = [tuple(v) for v in visits_of(conn, pid)]
    first = nr.classify_legacy(conn)
    again = nr.classify_legacy(conn)
    assert first == {"legacy_typed": 1, "legacy_pending": 1, "skipped_erased": 1}, first
    assert again == {"legacy_typed": 0, "legacy_pending": 0, "skipped_erased": 1}, again
    assert [tuple(v) for v in visits_of(conn, pid)] == snapshot, "11: A LEGACY VISIT WAS REWRITTEN"
    legacy = conn.execute("SELECT * FROM note_reviews WHERE origin = 'legacy'").fetchone()
    assert legacy["status"] == "pending" and legacy["patient_id"] == pid

    # 12. summaries use reviewed notes only, and say what they left out
    import visit_summary as vs
    ids = {s["id"] for s in vs.sources(conn, pid)[0]}
    scan_old = conn.execute("SELECT id FROM visits WHERE source_path LIKE '%scan_old%'").fetchone()[0]
    assert scan_old not in ids, "12: an unreviewed legacy note reached a summary"
    assert vid in ids and typed_visit["id"] in ids
    sid = vs.generate(conn, pid, *D, now=t0)
    text = " ".join(l["text"] for l in vs.load(conn, sid, pid, *D)["versions"][-1]["lines"])
    assert "awaiting review" in text, "12: the summary does not say notes were left out"
    nr.confirm(conn, legacy["id"], {}, *D, sorted_root=sorted_root, collection=collection, now=t0)
    assert scan_old in {s["id"] for s in vs.sources(conn, pid)[0]}, "12: a confirmed legacy note"
    assert vs.load(conn, sid, pid, *D)["stale"] is True, "12: reviewing a note outdates the summary"
    assert conn.execute("SELECT method FROM visit_reviews WHERE visit_id = ?",
                        (scan_old,)).fetchone()[0] == "legacy_confirmed"

    # 13. retention reports staged notes and never deletes them
    import retention
    rows = {r["type"]: r for r in retention.plan(conn, retention.policy())}
    assert rows["staged_notes"]["sweep"] == "report", rows.get("staged_notes")

    # 14. merge moves reviews with the patient; backup carries staging/
    import backup
    import patient_identity
    assert "note_reviews" in patient_identity.MERGE_RELATIONS
    assert "staging" in backup.FILE_STORES, "14: staged uploads would be lost on restore"

    # 15. erasure removes the patient's reviews, staged files and review marks,
    # and nobody else's
    import erasure
    other_txt, other_js = routed(sorted_root, OTHER, "o1", raw="altra paziente")
    storage.sync_note_file(other_js, sorted_root, conn, collection, "assistant", "aassist")
    other_row = conn.execute("SELECT * FROM note_reviews WHERE patient_id = ?", (other,)).fetchone()
    # erase() reads the staged folders before the rows go, then removes them
    my_dirs = erasure._staging_dirs(conn, pid, [CF])
    assert my_dirs, "15: the patient's staged folders were not found"
    erasure._sqlite(conn, pid, CF, [], False)
    erasure._remove_staging(my_dirs)
    assert conn.execute("SELECT COUNT(*) FROM note_reviews WHERE patient_id = ? OR"
                        " codice_fiscale = ?", (pid, CF)).fetchone()[0] == 0, "15: reviews survived"
    assert conn.execute("SELECT COUNT(*) FROM visit_reviews r LEFT JOIN visits v ON v.id ="
                        " r.visit_id WHERE v.id IS NULL").fetchone()[0] == 0, "15: orphan marks"
    assert not any(Path(d).exists() for d in my_dirs), "15: staged files survived erasure"
    assert nr.staged_files(other_row)["original"].exists(), "15: erased someone else's upload"
    left = erasure.remaining(conn, pid, [CF], sorted_root, tmp / "undo.jsonl", None, False)
    assert left.get("note_reviews") == 0, left
    conn.close()


def pipeline(tmp):
    """The upload worker end to end: a .txt dropped, routed, and staged."""
    import upload_worker
    tmp = Path(tmp)
    upload_worker.SORTED_ROOT = tmp / "sorted"
    upload_worker.DB_PATH = str(tmp / "pipe.sqlite")
    upload_worker.CHROMA_PATH = str(tmp / "pipe_chroma")
    nr.STAGING_ROOT = tmp / "staging"
    conn = storage.init_db(upload_worker.DB_PATH)
    pid = patient_id.seed_patient(conn, CF, "Ugo Upload")
    conn.close()
    note = DentalNote(patient_name="Ugo Upload", codice_fiscale=CF, visit_date="2026-08-08",
                      procedures=["rct 26"], clinical_notes="rct 26")
    upload_worker._extract = lambda _t: note
    drop = tmp / "drop"
    drop.mkdir()
    good = drop / "good.txt"
    good.write_text(f"{CF} rct 26")
    upload_worker._process_one(str(good), "aassist", "assistant")

    def bad_extract(_t):
        raise ValueError("unreadable")
    upload_worker._extract = bad_extract
    bad = drop / "bad.txt"
    bad.write_text("???")
    upload_worker._process_one(str(bad), "aassist", "assistant")

    conn = storage.connect(upload_worker.DB_PATH)
    # 16. the worker stages the readable note and records the unreadable one
    states = sorted(r[0] for r in conn.execute("SELECT status FROM note_reviews"))
    assert states == ["extraction_failed", "pending"], states
    assert conn.execute("SELECT COUNT(*) FROM visits WHERE patient_id = ?",
                        (pid,)).fetchone()[0] == 0, "16: THE WORKER FILED AN UPLOAD"
    # 17. the uploader's intake list says it is waiting for review
    from app.upload_routes import _user_recent_intake
    got = {Path(r["target"]).name: r["state"] for r in _user_recent_intake(conn, "aassist")}
    assert got.get("good.txt") == "awaiting_review", got
    conn.close()


def exposure():
    # 18. no patient, public, phone, reminder, chat or export code knows about
    # staged notes, and the export only walks sorted/
    for path in [*ROOT.glob("patient_app/*.py"), *ROOT.glob("site_app/*.py"),
                 ROOT / "calls.py", ROOT / "reminders.py", ROOT / "patient_accessor.py",
                 ROOT / "ask.py", ROOT / "data_rights.py", ROOT / "patient_agent.py"]:
        text = path.read_text()
        assert "note_review" not in text and "staging" not in text, f"18: {path.name}"


def routes(tmp):
    from werkzeug.security import generate_password_hash

    import app.db as app_db
    import web_session
    from app import create_app

    tmp = Path(tmp)
    db_path = str(tmp / "routes.sqlite")
    app_db.DB_PATH = db_path
    app_db.CHROMA_PATH = str(tmp / "routes_chroma")
    nr.STAGING_ROOT = tmp / "routes_staging"
    sorted_root = tmp / "routes_sorted"
    import app.review_routes as review_routes
    review_routes.SORTED_ROOT = sorted_root
    app = create_app()
    app.config["TESTING"] = True
    conn = storage.init_db(db_path)
    pid = patient_id.seed_patient(conn, CF, "Ugo Upload")
    _txt, js = routed(sorted_root, CF, "r1")
    storage.sync_note_file(js, sorted_root, conn, storage.get_collection(app_db.CHROMA_PATH),
                           "assistant", "rv_assist")
    rid = conn.execute("SELECT id FROM note_reviews").fetchone()[0]

    def client(username, role):
        conn.execute("INSERT OR IGNORE INTO users (username, password_hash, role, active)"
                     " VALUES (?, ?, ?, 1)", (username, generate_password_hash("x"), role))
        conn.commit()
        c = app.test_client()
        c.set_cookie(web_session.COOKIE_NAME, web_session.create_session(conn, username, role))
        return c

    def csrf(html):
        return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)

    dentist, reception, admin = (client("rv_dentist", "dentist"), client("rv_assist", "assistant"),
                                 client("rv_admin", "admin"))
    # 19. only the dentist reaches the queue or a review
    for c in (reception, admin):
        assert c.get("/reviews").status_code == 302
        assert c.get(f"/reviews/{rid}").status_code == 302
    page = dentist.get(f"/reviews/{rid}").text
    assert "rct 26 1a seduta" in page, "19: the original text is not shown for review"
    assert "not interpreted" in page, "19: extracted codes are not labelled as uninterpreted"
    own = csrf(reception.get("/", follow_redirects=True).text)
    reception.post(f"/reviews/{rid}/confirm", data={"csrf_token": own})
    assert conn.execute("SELECT status FROM note_reviews WHERE id = ?", (rid,)).fetchone()[0] \
        == "pending", "19: reception confirmed through a direct POST"
    # 20. the dentist confirms through the form, and the note becomes a visit
    dentist.post(f"/reviews/{rid}/confirm", data={
        "csrf_token": csrf(page), "visit_date": "2026-05-05", "procedures": "rct 26",
        "clinical_notes": "rct 26 prima seduta", "next_appointment": ""})
    assert conn.execute("SELECT status FROM note_reviews WHERE id = ?", (rid,)).fetchone()[0] \
        == "confirmed"
    assert conn.execute("SELECT COUNT(*) FROM visits WHERE patient_id = ?", (pid,)).fetchone()[0] == 1
    conn.close()


def selftest():
    saved = nr.STAGING_ROOT
    try:
        with tempfile.TemporaryDirectory() as tmp:
            domain(tmp)
        with tempfile.TemporaryDirectory() as tmp:
            pipeline(tmp)
        exposure()
        with tempfile.TemporaryDirectory() as tmp:
            routes(tmp)
    finally:
        nr.STAGING_ROOT = saved
    print("selftest ok")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
    else:
        print("usage: python note_review_selftest.py --selftest")
        sys.exit(1)
