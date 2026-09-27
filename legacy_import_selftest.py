"""Legacy import (P25): the evaluation set, the read-only source, the gates, review and life cycle.

Runs on the fixed synthetic folder from legacy_fixtures.py in a temp directory. The
image cases need macOS sips, tesseract and sandbox-exec; without them those cases
are reported SKIPPED, never passed.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

import clinic_time
import documents as docs
import legacy_fixtures as fx
import legacy_import as li
import patient_files as pf

D, A, ADM = ("drossi", "dentist"), ("aassist", "assistant"), ("anadmin", "admin")
T0 = clinic_time.read_instant("2026-09-27T08:00:00+00:00")
TOOLS = all(shutil.which(t) for t in ("sips", "tesseract", "sandbox-exec"))


def setup(tmp):
    from storage import init_db
    tmp = Path(tmp)
    docs.DOC_ROOT = tmp / "documents"
    docs.DOC_CHROMA_PATH = str(tmp / "doc_chroma")
    docs._collection_cache.clear()
    docs.SLOT_DIR = tmp / "slots"
    li.STAGING_ROOT = tmp / "import_staging"
    (tmp / "db").mkdir()
    conn = init_db(str(tmp / "db" / "clinic.sqlite"))
    pids = fx.seed(conn)
    folder = tmp / "legacy"
    cases = fx.build(folder)
    fx.fill_clinic_ids(folder, pids)
    return conn, pids, folder, cases


def snapshot(folder):
    """Every entry under the source: bytes hash, size and mtime. Links are not followed."""
    out = {}
    for path in sorted(Path(folder).rglob("*")):
        st = os.lstat(path)
        if path.is_symlink():
            out[str(path)] = ("link", os.readlink(path), st.st_mtime_ns)
        elif path.is_file():
            out[str(path)] = (hashlib.sha256(path.read_bytes()).hexdigest(), st.st_size, st.st_mtime_ns)
    return out


def counts(conn):
    tables = ("patient_documents", "import_batches", "import_items", "document_publications")
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


def by_case(conn, batch, cases):
    rows = {r["rel_path"]: r for r in li.items(conn, batch)}
    return {case: rows.get(rel) for case, (rel, _exp) in cases.items()}


def evaluation(tmp):
    # 1. the fixed evaluation set against the thresholds written in P25.md before the first run
    conn, pids, folder, cases = setup(tmp)
    before = snapshot(folder)
    report = li.dry_run(conn, folder, now=T0)
    assert counts(conn) == {"patient_documents": 0, "import_batches": 0, "import_items": 0,
                            "document_publications": 0}, "1: a dry run writes no row"
    assert not Path(li.STAGING_ROOT).exists() or not any(Path(li.STAGING_ROOT).rglob("*")), \
        "1: a dry run stages no file"
    batch = li.stage(conn, folder, *D, now=T0)
    assert snapshot(folder) == before, "1: HARD FAIL - the source folder changed"
    rows = by_case(conn, batch, cases)
    dry = {r["rel"]: r for r in report["items"]}
    lines, strong_wrong, missed_safety, recall_hit = [], 0, [], 0
    recall_set = ("E01", "E02", "E03", "E04", "E06", "E14", "E20")
    for case in sorted(cases):
        rel, (want_state, want_strength, want_who) = cases[case]
        r = rows[case]
        if r is None:
            got = ("missing", None, None)
        else:
            who = next((k for k, p in pids.items() if p == r["patient_id"]), None)
            got = (r["state"], r["strength"], who)
        assert dry[rel]["result"] == got[0], f"1: dry run and staging disagree on {case}"
        lines.append(f"{case} {rel} expected {want_state}/{want_strength}/{want_who} got {got[0]}/{got[1]}/{got[2]}")
        if got[1] == "strong" and got[2] != want_who:
            strong_wrong += 1
        if want_state in ("conflict", "unmatched", "refused") and got[0] == "proposed":
            missed_safety.append(case)
        if case in recall_set and got[0] == "proposed" and got[2] == want_who:
            recall_hit += 1
        if want_state == "refused":
            assert got[0] == "refused", f"1: {case} should be refused, got {got}"
        # regression pins added after the first (passing) run - stricter than the fixed thresholds:
        # every case keeps its expected result, and no proposal is stronger than its evidence
        assert got[0] == want_state, f"1: {case} expected {want_state}, got {got}"
        assert not (got[1] == "strong" and want_strength != "strong"), f"1: {case} is stronger than its evidence"
    present = [c for c in recall_set if c in cases]
    recall = recall_hit / len(present)
    print("\n".join("  " + l for l in lines))
    print(f"  strong precision wrong={strong_wrong}; safety misses={missed_safety};"
          f" proposal recall {recall_hit}/{len(present)} = {recall:.2f}")
    assert strong_wrong == 0, "1: HARD FAIL - a strong proposal names the wrong patient"
    assert not missed_safety, f"1: HARD FAIL - expected conflict/unmatched came out proposed: {missed_safety}"
    assert recall >= 0.85, f"1: proposal recall {recall:.2f} below the fixed 0.85"
    assert counts(conn)["patient_documents"] == 0, "1: HARD FAIL - a file was attached without confirmation"
    if not TOOLS:
        print("SKIPPED 1: image cases E04 E08 E12 E15 E15b E19 (sips, tesseract or sandbox-exec missing)")
    return conn, pids, folder, cases, batch, rows


def gates(conn, pids, folder, cases, batch, rows):
    # 2. what each gate did, and why
    e05 = li.evidence(rows["E05"])
    assert set(e05["candidates"]) == {pids["bianchi_a"], pids["bianchi_b"]}, \
        "2: same-name patients are both shown to the reviewer"
    assert rows["E05"]["patient_id"] is None, "2: a name that fits two people proposes nobody"
    e06 = li.evidence(rows["E06"])
    assert rows["E06"]["patient_id"] == pids["bianchi_a"] and rows["E06"]["strength"] == "check", \
        "2: name + birth date suggests one Bianchi, never strong"
    assert any(c["type"] == "birth_date" and c["where"] for c in e06["clues"]), "2: the birth date says where it came from"
    e01 = li.evidence(rows["E01"])
    cf_clue = next(c for c in e01["clues"] if c["type"] == "cf")
    assert cf_clue["value"] == fx.CF["rossi"] and cf_clue["source"] == "text" and cf_clue["labelled"] \
        and "line" in cf_clue["where"], "2: the identifier shows its exact source"
    e10 = li.evidence(rows["E10"])
    bad = next(c for c in e10["clues"] if c["type"] == "cf")
    assert bad["valid"] is False and bad["patient"] is None, "2: a failed checksum is never used"
    e11 = li.evidence(rows["E11"])
    assert any("not a patient" in c for c in e11["conflicts"]), "2: an unknown valid code is a conflict"
    e13 = li.evidence(rows["E13"])
    assert e13["leads"] and not e13["candidates"], "2: a folder name is a weak lead, never a candidate"
    e09 = li.evidence(rows["E09"])
    assert {pids["rossi"], pids["neri"]} <= set(e09["candidates"]), "2: both people in one PDF are shown"
    e16 = rows["E16"]
    assert "DICOM" in (e16["reason"] or "") and "not read" in e16["reason"], \
        "2: DICOM is refused and says why, whatever its filename claims"
    assert e16["patient_id"] is None and not li.staged_file(e16).exists(), "2: a refused file is not staged"
    e18 = rows["E18"]
    assert e18["state"] == "refused" and "link" in e18["reason"], "2: a symbolic link is refused"
    for case in ("E12", "E13", "E10"):
        assert rows[case]["patient_id"] is None, f"2: {case} has no candidate"
    if TOOLS:
        e08 = li.evidence(rows["E08"])
        sources = {c["source"] for c in e08["clues"] if c["type"] == "cf"}
        assert sources == {"ocr", "path"}, f"2: E08 shows both the OCR and filename sources, got {sources}"
        assert rows["E04"]["strength"] == "check", "2: an identifier only in a filename is never strong"
        e19 = li.evidence(rows["E19"])
        assert "clock" in " ".join(e19["flags"]), "2: a mis-set device clock is flagged"
        assert e19["acquired_basis"] == "document_date" and e19["acquired_at"].startswith("2021-06-1"), \
            "2: a suspect EXIF time is never the acquisition date"
        assert rows["E15"]["state"] == rows["E15b"]["state"] == "conflict", \
            "2: the same bytes for two patients are a conflict, both copies"
    # 2b. times never decide identity: change the file time and re-read, same result
    target = folder / cases["E13"][0]
    os.utime(target, (1_000_000_000, 1_000_000_000))
    again = li.dry_run(conn, folder, now=T0)
    same = {r["rel"]: r["result"] for r in again["items"]}
    assert same[cases["E13"][0]] == "unmatched" and same[cases["E01"][0]] == "proposed", \
        "2b: a changed file time changes nothing"
    assert counts(conn)["import_items"] == len(li.items(conn, batch)), "2b: a dry run adds nothing"


def times():
    # 3. Europe/Rome, stated offsets, the DST gap and a declared clock correction
    t = li.exif_time("2021:03:04 10:15:00", None, 0)
    assert t["utc"] == "2021-03-04T09:15:00+00:00" and "assumed" in t["zone"], \
        "3: EXIF with no offset is read as clinic time, and says so"
    t = li.exif_time("2021:07:04 10:15:00", "+02:00", 0)
    assert t["utc"] == "2021-07-04T08:15:00+00:00" and "stated" in t["zone"], "3: a stated offset wins"
    t = li.exif_time("2021:03:28 02:30:00", None, 0)
    assert t["utc"] is None and "does not exist" in t["problem"], "3: a time in the spring-forward gap is refused"
    t = li.exif_time("2019:01:01 00:03:00", None, 3600)
    assert t["utc"] == "2019-01-01T00:03:00+00:00" and t["corrected"], "3: a declared correction is applied and labelled"
    assert li.exif_time("0000:00:00 00:00:00", None, 0)["utc"] is None, "3: a blank camera time is unknown"
    acquired, basis, _shown, flags = li._times([], {"original": "1990:01:01 10:00:00"}, None, 0, T0)
    assert acquired is None and basis is None and "implausible" in " ".join(flags), \
        "3: an implausible camera time is flagged and never used as the date"
    acquired, basis, _shown, flags = li._times([], {"original": "2021:03:04 10:15:00"}, None, 0, T0)
    assert basis == "exif_original" and "unconfirmed" in " ".join(flags), \
        "3: a lone plausible camera time is used, and labelled unconfirmed"


def review(conn, pids, folder, cases, batch, rows):
    # 4. confirmation: dentist only, rechecked, audited, the only way in
    e01 = rows["E01"]
    for who in (A, ADM):
        try:
            li.confirm(conn, e01["id"], fx.CF["rossi"], *who, expected_sha=e01["sha256"])
            raise AssertionError(f"4: {who[1]} confirmed an identity")
        except PermissionError:
            pass
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'import_confirm' AND allowed = 0"
                        ).fetchone()[0] == 2, "4: refused confirmations are audited"
    try:
        li.confirm(conn, e01["id"], fx.CF["rossi"], *D, expected_sha="0" * 64)
        raise AssertionError("4: confirmed a file other than the one reviewed")
    except li.ImportProblem as e:
        assert e.code == "changed", f"refused for the wrong reason: {e.code}"
    # a conflict needs a reason; a different patient than proposed needs a reason
    e07 = rows["E07"]
    try:
        li.confirm(conn, e07["id"], fx.CF["verdi"], *D, expected_sha=e07["sha256"])
        raise AssertionError("4: a conflict was confirmed without a reason")
    except li.ImportProblem as e:
        assert e.code == "reason_needed", f"refused for the wrong reason: {e.code}"
    # repeated visits: a suggestion is offered, never applied
    v = [conn.execute("INSERT INTO visits (patient_id, visit_date, procedures, source_path) VALUES"
                      " (?, ?, '[]', ?)", (pids["rossi"], d, f"fx/{d}.json")).lastrowid
         for d in ("2021-03-04", "2021-09-10", "2022-01-01")]
    other = conn.execute("INSERT INTO visits (patient_id, visit_date, procedures, source_path) VALUES"
                         " (?, '2021-03-04', '[]', 'fx/other.json')", (pids["verdi"],)).lastrowid
    conn.commit()
    suggested = li.suggested_visits(conn, e01, pids["rossi"])
    assert [s["id"] for s in suggested] == [v[0]], "4: the visit on the document's date is suggested"
    try:
        li.confirm(conn, e01["id"], fx.CF["rossi"], *D, expected_sha=e01["sha256"], visit_id=other)
        raise AssertionError("4: a file was tied to another patient's visit")
    except li.ImportProblem as e:
        assert e.code == "visit", f"refused for the wrong reason: {e.code}"
    # a tampered staged copy is refused at confirmation
    staged = li.staged_file(rows["E03"])
    keep = staged.read_bytes()
    os.chmod(staged, 0o600)
    staged.write_bytes(keep + b" ")
    try:
        li.confirm(conn, rows["E03"]["id"], fx.CF["rossi"], *D, expected_sha=rows["E03"]["sha256"])
        raise AssertionError("4: a changed staged copy was confirmed")
    except li.ImportProblem as e:
        assert e.code == "changed", f"refused for the wrong reason: {e.code}"
    staged.write_bytes(keep)
    assert conn.execute("SELECT COUNT(*) FROM patient_documents").fetchone()[0] == 0, \
        "4: HARD FAIL - something attached before a confirmation succeeded"

    doc = li.confirm(conn, e01["id"], fx.CF["rossi"], *D, expected_sha=e01["sha256"], now=T0)
    d = docs.row(conn, doc)
    assert d["patient_id"] == pids["rossi"] and d["status"] == "confirmed" and d["source"] == "legacy_import" \
        and d["visit_id"] is None and d["decided_by"] == "drossi", "4: confirmed, attached, no visit unless chosen"
    assert d["acquired_at"].startswith("2021-03-04") and d["acquired_basis"] == "document_date"
    assert docs.original_path(d).read_bytes() == (folder / cases["E01"][0]).read_bytes(), \
        "4: HARD FAIL - the attached original is not byte-identical"
    assert not pf.published(conn, doc, pids["rossi"]), "4: HARD FAIL - confirmation published the file"
    # the duplicate copy links to the same document, never a second one
    e14 = rows["E14"]
    assert li.confirm(conn, e14["id"], fx.CF["rossi"], *D, expected_sha=e14["sha256"]) == doc, \
        "4: duplicate bytes for the same patient are one document"
    assert conn.execute("SELECT COUNT(*) FROM patient_documents").fetchone()[0] == 1
    try:
        li.confirm(conn, e01["id"], fx.CF["rossi"], *D, expected_sha=e01["sha256"])
        raise AssertionError("4: a decided item was confirmed twice")
    except li.ImportProblem as e:
        assert e.code == "decided", f"refused for the wrong reason: {e.code}"
    audit = conn.execute("SELECT target, reason FROM audit_log WHERE action LIKE 'import_%'").fetchall()
    for target, reason in audit:
        for secret in (fx.CF["rossi"], "Rossi", "referto"):
            assert secret not in (target or "") + (reason or ""), f"4: audit carries {secret!r}"
    # conflict with a reason: the reviewer's investigated decision stands and is recorded
    doc7 = li.confirm(conn, e07["id"], fx.CF["verdi"], *D, expected_sha=e07["sha256"],
                      reason="called the patient; the code on the report is a typing error")
    assert docs.row(conn, doc7)["patient_id"] == pids["verdi"]
    # identical bytes already confirmed for one patient, found again under another's name: a conflict
    if TOOLS:
        e04 = rows["E04"]
        li.confirm(conn, e04["id"], fx.CF["neri"], *D, expected_sha=e04["sha256"])
        # alone in its own folder, so only the record check - not the batch cross-check - can catch it
        lone = folder.parent / "lone"
        (lone / "copie").mkdir(parents=True)
        (lone / fx.MARKER).write_text("synthetic\n")
        (lone / "copie" / f"{fx.CF['rossi']}_opg.jpg").write_bytes((folder / cases["E04"][0]).read_bytes())
        found = {r["rel"]: r["result"] for r in li.dry_run(conn, lone, now=T0)["items"]}
        shutil.rmtree(lone)
        assert found[f"copie/{fx.CF['rossi']}_opg.jpg"] == "conflict", \
            "4: bytes already in another patient's record are a conflict, never a proposal"
    # reject and hold
    li.hold(conn, rows["E13"]["id"], "ask reception who this is", *D)
    assert li.items(conn, batch, state="held")[0]["id"] == rows["E13"]["id"]
    li.reject(conn, rows["E10"]["id"], "unreadable code", *D)
    assert li.item(conn, rows["E10"]["id"])["state"] == "rejected"
    try:
        li.reject(conn, rows["E05"]["id"], "x", *A)
        raise AssertionError("4: the assistant rejected an item")
    except PermissionError:
        pass
    return doc


def concurrent(tmp):
    # 5. two dentists confirm the same item at the same moment: one document
    conn, pids, folder, cases = setup(tmp)
    batch = li.stage(conn, folder, *D, now=T0)
    e02 = by_case(conn, batch, cases)["E02"]
    db = conn.execute("PRAGMA database_list").fetchone()[2]
    outcomes, barrier = [], threading.Barrier(2)

    def go():
        from storage import connect
        c = connect(db)
        barrier.wait()
        try:
            outcomes.append(li.confirm(c, e02["id"], fx.CF["verdi"], *D, expected_sha=e02["sha256"]))
        except li.ImportProblem as e:
            outcomes.append(e.code)
        finally:
            c.close()
    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(outcomes) == 2 and outcomes.count("decided") == 1, outcomes
    assert conn.execute("SELECT COUNT(*) FROM patient_documents").fetchone()[0] == 1, \
        "5: HARD FAIL - a concurrent confirmation made two documents"
    conn.close()


def interrupted(tmp):
    # 6. a crash mid-import, and a crash mid-confirmation, leave nothing half-done
    conn, pids, folder, cases = setup(tmp)
    real = li._assess
    calls = {"n": 0}

    def crash(*a, **k):
        calls["n"] += 1
        if calls["n"] == 4:
            raise KeyboardInterrupt("power cut")
        return real(*a, **k)
    li._assess = crash
    try:
        li.stage(conn, folder, *D, now=T0)
        raise AssertionError("6: the crash did not happen")
    except KeyboardInterrupt:
        pass
    finally:
        li._assess = real
    first = conn.execute("SELECT id, status FROM import_batches").fetchone()
    assert first["status"] == "running", "6: an interrupted batch is visibly unfinished"
    batch = li.stage(conn, folder, *D, now=T0)
    li.stage(conn, folder, *D, now=T0)
    total = conn.execute("SELECT COUNT(*) FROM import_items").fetchone()[0]
    assert total == len(cases), f"6: re-runs made duplicate items ({total} for {len(cases)} files)"
    assert all(r["state"] not in ("discovered", "staged") for r in li.items(conn, None)), \
        "6: every interrupted item was finished on resume"
    found = li.reconcile(conn)
    assert found["orphan_files"] == 0 and found["missing"] == [] and found["damaged"] == [], found
    # a stray staged file from a crash is found and removed only on apply
    stray = Path(li.STAGING_ROOT) / ("f" * 64)
    stray.write_bytes(b"stray")
    assert li.reconcile(conn)["orphan_files"] == 1 and stray.exists()
    li.reconcile(conn, apply=True)
    assert not stray.exists(), "6: reconcile --apply removes the stray copy"
    # confirmation crashes after the file is written: no row, no file, still reviewable
    e01 = by_case(conn, batch, cases)["E01"]
    real_mark = li._mark_confirmed

    def boom(*a, **k):
        # the original is already written and its row inserted when this fails
        assert any((Path(docs.DOC_ROOT) / pids["rossi"]).iterdir()), "6: the crash came too early"
        raise sqlite3.OperationalError("disk I/O error")
    li._mark_confirmed = boom
    try:
        li.confirm(conn, e01["id"], fx.CF["rossi"], *D, expected_sha=e01["sha256"])
        raise AssertionError("6: the confirmation crash did not happen")
    except sqlite3.OperationalError:
        pass
    finally:
        li._mark_confirmed = real_mark
    assert conn.execute("SELECT COUNT(*) FROM patient_documents").fetchone()[0] == 0, "6: a row survived the crash"
    assert not (Path(docs.DOC_ROOT) / pids["rossi"]).exists() or not any(
        (Path(docs.DOC_ROOT) / pids["rossi"]).iterdir()), "6: a file survived the crash"
    assert li.item(conn, e01["id"])["state"] == "proposed", "6: the item is still waiting"
    assert li.confirm(conn, e01["id"], fx.CF["rossi"], *D, expected_sha=e01["sha256"])
    conn.close()


def guards(tmp):
    # 7. no marker, no read; an assistant sees progress, never contents
    conn, pids, folder, cases = setup(tmp)
    plain = Path(tmp) / "shared"
    plain.mkdir()
    (plain / "a.txt").write_text(f"Codice fiscale: {fx.CF['rossi']}\n")
    for fn in (lambda: li.dry_run(conn, plain), lambda: li.stage(conn, plain, *D)):
        try:
            fn()
            raise AssertionError("7: a folder without the synthetic marker was read")
        except li.ImportProblem as e:
            assert e.code == "not_authorised", f"refused for the wrong reason: {e.code}"
    try:
        li.stage(conn, folder, *ADM)
        raise AssertionError("7: admin ran an import")
    except PermissionError:
        pass
    batch = li.stage(conn, folder, *A, now=T0)
    progress = li.progress(conn, *A)
    text = json.dumps(progress)
    assert progress and progress[0]["counts"], "7: the assistant sees the batch's progress"
    for secret in (fx.CF["rossi"], "Rossi", "Bianchi", "referti", ".txt"):
        assert secret not in text, f"7: the assistant's progress view carries {secret!r}"
    for fn in (lambda: li.queue(conn, *A), lambda: li.load_item(conn, 1, *A)):
        try:
            fn()
            raise AssertionError("7: the assistant opened import contents")
        except PermissionError:
            pass
    try:
        li.progress(conn, *ADM)
        raise AssertionError("7: admin saw import progress")
    except PermissionError:
        pass
    assert batch
    conn.close()


def lifecycle(tmp):
    # 8. merge, erasure, backup and restore keep the rules
    conn, pids, folder, cases = setup(tmp)
    batch = li.stage(conn, folder, *D, now=T0)
    rows = by_case(conn, batch, cases)
    doc = li.confirm(conn, rows["E01"]["id"], fx.CF["rossi"], *D, expected_sha=rows["E01"]["sha256"])
    pf.publish(conn, doc, pids["rossi"], *D)
    doc_neri = li.confirm(conn, rows["E20"]["id"], fx.CF["neri"], *D, expected_sha=rows["E20"]["sha256"])
    pf.publish(conn, doc_neri, pids["neri"], *D)

    # 8a. backup and restore: originals, staged copies and publication state come back
    import backup
    root = Path(tmp)
    key = root / "backup.key"
    backup.init_key(key)
    conn.commit()
    archive = backup.create(data_root=root, dest=root / "backups", key_path=key)["archive"]
    restored = root / "restored"
    backup.restore(archive, restored, key, apply=True)
    rconn = sqlite3.connect(restored / "db" / "clinic.sqlite")
    rconn.row_factory = sqlite3.Row
    r = rconn.execute("SELECT * FROM patient_documents WHERE id = ?", (doc,)).fetchone()
    assert (restored / "documents" / r["stored_path"]).read_bytes() == \
        docs.original_path(docs.row(conn, doc)).read_bytes(), "8a: the original is restored byte for byte"
    assert rconn.execute("SELECT COUNT(*) FROM document_publications WHERE document_id = ? AND"
                         " withdrawn_at IS NULL", (doc,)).fetchone()[0] == 1, "8a: publication state restored"
    staged = rconn.execute("SELECT sha256 FROM import_items WHERE state = 'conflict' LIMIT 1").fetchone()
    assert (restored / "import_staging" / staged[0]).exists(), "8a: staged copies are in the backup"
    rconn.close()

    # 8b. merge: the moved record's publications are withdrawn, staged proposals follow
    import patient_identity
    ok, message = patient_identity.merge(conn, fx.CF["rossi"], fx.CF["verdi"], "anadmin", "admin",
                                         sorted_root=root / "sorted")
    assert ok, message
    assert docs.row(conn, doc)["patient_id"] == pids["verdi"], "8b: the document moved with the record"
    assert not pf.published(conn, doc, pids["verdi"]), \
        "8b: a merge never republishes a file under the surviving record"
    assert conn.execute("SELECT COUNT(*) FROM document_publications WHERE document_id = ? AND withdrawn_at"
                        " IS NULL", (doc,)).fetchone()[0] == 0, "8b: the merge withdrew the publication itself"
    assert conn.execute("SELECT COUNT(*) FROM import_items WHERE patient_id = ?", (pids["rossi"],)
                        ).fetchone()[0] == 0, "8b: proposals for the folded record follow it"

    # 8c. erasure: documents, publications, staged items and their copies go
    import erasure
    staged_neri = [li.staged_file(r) for r in li.items(conn, batch) if r["patient_id"] == pids["neri"]]
    assert staged_neri
    erasure.erase(conn, pids["neri"], "drossi", "dentist", req_id=None, sorted_root=root / "sorted",
                  drop_dir=root / "drop", undo_log=root / "undo.jsonl", exports_dir=root / "exports",
                  tombstones=root / "tombstones", write_tombstone=False)
    for table, col in (("patient_documents", "patient_id"), ("document_publications", "patient_id"),
                       ("import_items", "patient_id")):
        n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} = ?", (pids["neri"],)).fetchone()[0]
        assert n == 0, f"8c: erasure left {n} row(s) in {table}"
    assert not any(p.exists() for p in staged_neri if not any(
        r["sha256"] == p.name for r in li.items(conn, None))), "8c: erased staged copies are gone"
    conn.close()


def selftest():
    for fn in (evaluation_and_review, lambda t: concurrent(t), lambda t: interrupted(t),
               lambda t: guards(t), lambda t: lifecycle(t)):
        with tempfile.TemporaryDirectory() as tmp:
            fn(tmp)
    times()
    print("legacy_import_selftest: ok")


def evaluation_and_review(tmp):
    conn, pids, folder, cases, batch, rows = evaluation(tmp)
    gates(conn, pids, folder, cases, batch, rows)
    review(conn, pids, folder, cases, batch, rows)
    conn.close()


if __name__ == "__main__":
    selftest()
    sys.exit(0)
