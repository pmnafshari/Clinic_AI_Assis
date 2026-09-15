"""Phase 51 acceptance: merge consistency across every store, and recovery.

This is the file that decides whether Phase 51 is done. The migration has its
own proof in migrate_pid_selftest; what is proved HERE is the thing P04 left
open and P51 promised to close - that after the surrogate migration a MERGE
stays consistent across SQLite, the alias mapping, the Chroma metadata, search
results, the filesystem, stored source paths and historical links, and that an
interruption between those stores is recoverable rather than silent.

NO DISTRIBUTED ATOMICITY IS CLAIMED ANYWHERE IN THIS FILE. SQLite, Chroma and
the filesystem cannot share a transaction. What is asserted instead is the
weaker, true property: after any injected failure the system is in a state that
is RECORDED, RESUMABLE and never attributes one patient's data to another.
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

import chromadb
from chromadb.config import Settings

import migrate_pid
import patient_id
import patient_identity
import storage

KEEP_CF, GONE_CF, OTHER_CF = "RSSM800010150100", "RSSP800010150100", "BNCG850010150900"
RELATIONS = ("visits", "invoices", "appointments", "patient_credentials", "patient_sessions")


def _fixture(tmp, name="acc"):
    """Two lookalike patients plus an unrelated third, on the migrated schema."""
    conn = storage.init_db(str(tmp / f"{name}.sqlite"))
    root = tmp / f"sorted-{name}"
    pids = {}
    for cf, who, phone in ((KEEP_CF, "Mario Rossi", "333111"),
                           (GONE_CF, "mario rossi", "333111"),
                           (OTHER_CF, "Giulia Bianchi", None)):
        pid = patient_id.seed_patient(conn, cf, who, phone)
        pids[cf] = pid
        conn.execute(
            "INSERT INTO visits (patient_id, visit_date, procedures, clinical_notes,"
            " next_appointment, source_path) VALUES (?, '2026-03-02', '[\"comp 20\"]',"
            " ?, NULL, ?)", (pid, f"note for {who}", f"sorted/{pid}/notes/n1.json"))
        vid = conn.execute("SELECT id FROM visits WHERE patient_id = ?", (pid,)).fetchone()["id"]
        conn.execute("INSERT INTO invoices (patient_id, visit_id, line_index, amount,"
                     " description) VALUES (?, ?, 0, 80.0, ?)", (pid, vid, f"bill {who}"))
        conn.execute(
            "INSERT INTO appointments (patient_id, dentist, starts_at, minutes, status,"
            " created_at, updated_at) VALUES (?, ?, ?, 30, 'booked', '', '')",
            (pid, f"dr {cf[:2].lower()}", f"2026-06-0{1 + len(pids)}T09:00:00"))
        conn.execute("INSERT INTO patient_credentials (patient_id, pin_hash, issued_at,"
                     " expires_at) VALUES (?, 'h', '', '')", (pid,))
        conn.execute("INSERT INTO patient_sessions (token_hash, patient_id, created_at,"
                     " last_seen_at) VALUES (?, ?, '', '')", (f"tok-{pid}", pid))
        # the patient's own directory and the note file its source_path names
        (root / pid / "notes").mkdir(parents=True, exist_ok=True)
        (root / pid / "notes" / "n1.json").write_text(json.dumps({"who": who}))
        (root / pid / "images").mkdir(parents=True, exist_ok=True)
        (root / pid / "images" / "xray.jpg").write_text(who)
    conn.commit()
    return conn, root, pids


def _collection(tmp, pids, name="acc"):
    client = chromadb.PersistentClient(path=str(tmp / f"chroma-{name}"),
                                       settings=Settings(anonymized_telemetry=False))
    coll = client.get_or_create_collection(name="patient_notes")
    for cf, pid in pids.items():
        coll.upsert(ids=[f"{pid}:n1"], documents=[f"a filling for {cf}"],
                    metadatas=[{"patient_id": pid, "patient_name": cf,
                                "source_path": f"sorted/{pid}/notes/n1.json"}])
    return coll


def _counts(conn, pid):
    return {t: conn.execute(f"SELECT COUNT(*) c FROM {t} WHERE patient_id = ?",
                            (pid,)).fetchone()["c"] for t in RELATIONS}


def _orphans(conn):
    return {t: conn.execute(
        f"SELECT COUNT(*) c FROM {t} x LEFT JOIN patients p ON p.patient_id = x.patient_id"
        " WHERE p.patient_id IS NULL").fetchone()["c"] for t in RELATIONS}


def test_7(tmp):
    """Merge preserves every relationship in all six tables, with no orphans
    and no change to anyone else."""
    conn, root, pids = _fixture(tmp, "t7")
    keep, gone, other = pids[KEEP_CF], pids[GONE_CF], pids[OTHER_CF]
    before_keep, before_gone, before_other = _counts(conn, keep), _counts(conn, gone), _counts(conn, other)
    assert all(v == 1 for v in before_gone.values()), f"T7 setup: {before_gone}"

    ok, msg = patient_identity.merge(conn, GONE_CF, KEEP_CF, "anadmin", "admin",
                                     sorted_root=root)
    assert ok, f"T7: the merge should succeed - {msg}"

    after_keep = _counts(conn, keep)
    for table in RELATIONS:
        if table == "patient_credentials":
            # revoked and deleted, never repointed: the column is UNIQUE and a
            # folded identity must not keep its own way to sign in
            assert after_keep[table] == 1, \
                f"T7: the survivor keeps its own credential, got {after_keep[table]}"
            continue
        assert after_keep[table] == before_keep[table] + before_gone[table], \
            f"T7: {table} did not move - {before_keep[table]}+{before_gone[table]} -> {after_keep[table]}"
    assert _counts(conn, gone) == {t: 0 for t in RELATIONS}, \
        "T7: nothing may be left pointing at the folded patient"

    # THE UNRELATED PATIENT IS THE POINT. a merge that quietly touched a third
    # record would pass every count above.
    assert _counts(conn, other) == before_other, \
        "T7: AN UNRELATED PATIENT MUST NOT BE TOUCHED"
    assert _orphans(conn) == {t: 0 for t in RELATIONS}, "T7: the merge must leave no orphans"
    assert not conn.execute("PRAGMA foreign_key_check").fetchall(), \
        "T7: foreign keys must still be intact"
    assert conn.execute("SELECT COUNT(*) c FROM patients WHERE patient_id = ?",
                        (gone,)).fetchone()["c"] == 0, "T7: the folded patients row is gone"
    conn.close()
    print("  T7 pass: six relations preserved, no orphans, no cross-patient change")


def test_8(tmp):
    """Live, old and folded aliases all resolve to the final surrogate;
    constraints hold and a chain flattens."""
    conn, root, pids = _fixture(tmp, "t8")
    keep, gone, other = pids[KEEP_CF], pids[GONE_CF], pids[OTHER_CF]
    patient_identity.merge(conn, GONE_CF, KEEP_CF, "anadmin", "admin", sorted_root=root)

    # a LIVE codice fiscale, the SURROGATE itself, and a FOLDED codice fiscale
    assert patient_id.resolve(conn, KEEP_CF) == keep, "T8: a live CF resolves"
    assert patient_id.resolve(conn, keep) == keep, "T8: the surrogate resolves to itself"
    assert patient_id.resolve(conn, GONE_CF) == keep, \
        "T8: a FOLDED CF must resolve to the survivor, not 404"
    assert patient_id.resolve(conn, "ZZZZ999999999999") is None, "T8: an unknown CF is nobody"

    # an OLD codice fiscale that was corrected rather than merged
    conn.execute("UPDATE patients SET codice_fiscale = 'RSSM800010150199' WHERE patient_id = ?",
                 (keep,))
    conn.commit()
    assert patient_id.resolve(conn, "RSSM800010150199") == keep, "T8: the corrected CF resolves"
    assert patient_id.resolve(conn, keep) == keep, "T8: and identity never moved"
    assert patient_id.resolve(conn, GONE_CF) == keep, \
        "T8: the folded alias still resolves after the survivor's CF changed"
    assert patient_id.resolve(conn, KEEP_CF) is None, \
        "T8: the replaced CF no longer names a live patient"

    # the alias row carries the surrogate and has no foreign key on a CF -
    # that FK was what made a codice fiscale correction impossible
    row = conn.execute("SELECT target_patient_id FROM patient_merges WHERE source_cf = ?",
                       (GONE_CF,)).fetchone()
    assert row["target_patient_id"] == keep, "T8: the alias maps to the surrogate"
    fks = [f[2] for f in conn.execute("PRAGMA foreign_key_list(patient_merges)")]
    assert "patients" in fks, "T8: the alias still references patients"
    assert not any(f[4] == "codice_fiscale"
                   for f in conn.execute("PRAGMA foreign_key_list(patient_merges)")), \
        "T8: but NEVER on codice_fiscale - that key is what froze the CF"

    # CHAIN FLATTENING. staff merge A into B, later find B duplicates C.
    ok, msg = patient_identity.merge(conn, "RSSM800010150199", OTHER_CF, "anadmin", "admin",
                                     sorted_root=root)
    assert ok, f"T8: a survivor may be merged onward - {msg}"
    assert patient_id.resolve(conn, "RSSM800010150199") == other, "T8: the survivor moved on"
    assert patient_id.resolve(conn, GONE_CF) == other, \
        "T8: AND WHAT IT HAD ABSORBED FOLLOWS - one hop, never a dangling alias"
    assert set(patient_identity.merged_sources_of(conn, other)) == {GONE_CF, "RSSM800010150199"}, \
        "T8: the final survivor knows everything it holds"
    assert not conn.execute("PRAGMA foreign_key_check").fetchall(), "T8: FKs intact after a chain"
    conn.close()
    print("  T8 pass: live/old/folded aliases resolve, constraints hold, chain flattens")


def merge1_end_to_end(tmp):
    """MERGE-1 across every store named in the acceptance list."""
    conn, root, pids = _fixture(tmp, "m1")
    keep, gone = pids[KEEP_CF], pids[GONE_CF]
    coll = _collection(tmp, pids, "m1")
    gone_file = root / gone / "notes" / "n1.json"
    assert gone_file.exists(), "M1 setup: the folded patient has a file"

    ok, _ = patient_identity.merge(conn, GONE_CF, KEEP_CF, "anadmin", "admin",
                                   collection=coll, sorted_root=root)
    assert ok, "M1: the merge should succeed"
    results = {}

    # 1. SQLite
    results["sqlite"] = (_counts(conn, gone) == {t: 0 for t in RELATIONS}
                         and _orphans(conn) == {t: 0 for t in RELATIONS})
    # 2. alias mapping
    results["aliases"] = patient_id.resolve(conn, GONE_CF) == keep
    # 3. Chroma metadata
    left = coll.get(where={"patient_id": gone})
    results["chroma_metadata"] = left["ids"] == []
    # 4. search results - a scoped query returns the survivor's chunks and
    #    NOTHING attributed to the folded identity
    kept = coll.get(where={"patient_id": keep})
    results["search"] = (len(kept["ids"]) == 2
                         and all(m["patient_id"] == keep for m in kept["metadatas"]))
    # 5. filesystem directories
    results["filesystem"] = (not (root / gone).exists()) and (root / keep).is_dir()
    # 6. stored source paths
    paths = [r["source_path"] for r in conn.execute(
        "SELECT source_path FROM visits WHERE patient_id = ?", (keep,))]
    results["source_path"] = all(gone not in p for p in paths) and len(paths) == 2
    #    ...and every stored path names a file that is actually there
    import agent
    results["files_resolve"] = all(
        agent.note_json_path(p, root, keep).exists() for p in paths)
    # 7. historical links
    results["historical_links"] = (patient_identity.merge_target(conn, GONE_CF) is not None
                                   and patient_id.resolve(conn, GONE_CF) == keep)

    for name, passed in results.items():
        print(f"    {'PASS' if passed else 'FAIL'}  {name}")
    assert all(results.values()), f"MERGE-1 end to end: {results}"
    conn.close()
    print("  MERGE-1 pass: consistent across every store in the acceptance list")


def failure_injection(tmp):
    """Interrupt between the stores; prove recorded, resumable, never wrong."""
    conn, root, pids = _fixture(tmp, "fi")
    keep, gone, other = pids[KEEP_CF], pids[GONE_CF], pids[OTHER_CF]
    coll = _collection(tmp, pids, "fi")

    # --- interruption BEFORE the filesystem step (collection given, root not)
    ok, _ = patient_identity.merge(conn, GONE_CF, KEEP_CF, "anadmin", "admin",
                                   collection=coll, sorted_root=None)
    assert ok, "FI: sqlite and chroma complete even though the files cannot move yet"
    pend = conn.execute(
        "SELECT subject, payload, state FROM migration_ops WHERE migration = 'merge_files'"
        " AND state = 'pending'").fetchall()
    assert len(pend) == 1 and pend[0]["payload"] == gone, \
        f"FI: the unfinished file move MUST be recorded, got {[dict(r) for r in pend]}"
    assert (root / gone).is_dir(), "FI: and nothing on disk was touched"
    # the half-done state must not have moved data to a third patient
    assert _counts(conn, other) == {t: 1 for t in RELATIONS}, \
        "FI: NO WRITE TO THE WRONG PATIENT while work is pending"

    # --- a failure DURING the move is recorded as failed, not lost
    real_move = shutil.move
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk went away mid-move")
        return real_move(src, dst)

    patient_identity.shutil.move = flaky
    try:
        patient_identity.move_merged_files(conn, root)
    finally:
        patient_identity.shutil.move = real_move
    failed = conn.execute(
        "SELECT detail FROM migration_ops WHERE migration = 'merge_files'"
        " AND state = 'failed'").fetchall()
    assert len(failed) == 1 and "disk went away" in failed[0]["detail"], \
        f"FI: the failure must be recorded with its reason, got {[dict(r) for r in failed]}"
    assert (root / gone).exists(), "FI: the source directory survives a failed move"

    # --- retry: idempotent, and it completes
    conn.execute("UPDATE migration_ops SET state = 'pending' WHERE state = 'failed'")
    conn.commit()
    done = patient_identity.move_merged_files(conn, root)
    assert done, "FI: the retry must do the work"
    assert not (root / gone).exists(), "FI: and finish the move"
    assert (root / keep / "notes" / "n1.json").exists(), "FI: the survivor has the files"
    assert patient_identity.move_merged_files(conn, root) == [], \
        "FI: running the repair again is a no-op"
    assert conn.execute(
        "SELECT COUNT(*) c FROM migration_ops WHERE migration = 'merge_files'"
        " AND state = 'pending'").fetchone()["c"] == 0, "FI: nothing left pending"
    assert _counts(conn, other) == {t: 1 for t in RELATIONS}, \
        "FI: and the unrelated patient is still untouched at the end"
    conn.close()
    print("  Failure injection pass: recorded, resumable, idempotent, never wrong-patient")


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        test_7(tmp)
        test_8(tmp)
        merge1_end_to_end(tmp)
        failure_injection(tmp)
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
