"""staff intake walk - seven notes through the real upload path, unstubbed model.

closes FLOW-2: eval_notes.py scores the model against a jsonl file and the fast
suite stubs it entirely. neither has ever put a note through
upload -> drop -> worker -> sort_files -> dental-notes -> review -> sqlite. this does.

POL-9 (2026-09-23): an upload is staged for a dentist's review and never filed
on its own. the extraction checks read the staged extraction; a dentist then
confirms one note in the browser, and only that one reaches visits.

also the first live check of phase 23's needs_review badge, which until now was
only exercised through the flask test client.

needs, before running:
    ollama serve
    .venv/bin/python run.py                            # staff app, port 5000
    .venv/bin/python -m playwright install chromium    # once

    .venv/bin/python e2e_intake_walk.py [--headed]

seeds ZZI* codici fiscali and a throwaway dentist, deletes both in a finally.
ZZI keeps these rows clear of e2e_chat_walk.py's ZZE* namespace so the two
walks cannot collide. deliberately NOT in run_selftests.sh - it needs a server,
a browser and a live model.
"""

import json
import os
import shutil
import re
import sqlite3
from datetime import datetime, timezone
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from werkzeug.security import generate_password_hash

import auth
import sort_files

STAFF_URL = "http://127.0.0.1:5000"
DB_PATH = "db/clinic.sqlite"
SORTED_ROOT = Path("sorted")

STAFF_USER = "zzi_walker"
STAFF_PASS = "walkpass1234"

# ^[A-Z]{4}[0-9]{12}$ - ZZI* is this script's namespace
CF_RCT = "ZZIA850010150401"
CF_FILL = "ZZIB850010150402"
CF_EXT = "ZZIC850010150403"
CF_SEAL = "ZZID850010150404"
CF_IGIENE = "ZZIE850010150405"
CF_IGIENE_MULTI = "ZZIF850010150406"
ALL_CFS = (CF_RCT, CF_FILL, CF_EXT, CF_SEAL, CF_IGIENE, CF_IGIENE_MULTI)

# case 6 carries no codice fiscale on purpose - extract_note raises and
# sort_files routes it to needs_review
BAD_NOTE_NAME = "zzi_unreadable.txt"

NOTES = [
    ("zzi_rct.txt", f"{CF_RCT} Mario Rossi, devitalizzazione dente 46, fu 1mo", ["rct 46"]),
    ("zzi_fill.txt", f"{CF_FILL} Anna Verdi, otturazione dente 47, fu 2wk", ["filling 47"]),
    ("zzi_ext.txt", f"{CF_EXT} Bruno Neri, estrazione dente 38, fu 1wk", ["ext 38"]),
    ("zzi_seal.txt", f"{CF_SEAL} Carla Bianchi, sigillatura dente 16, fu 3wk", ["seal 16"]),
    # igiene in first position - always worked, kept as the control that shows
    # position is what mattered
    ("zzi_igiene.txt", f"{CF_IGIENE} Davide Costa, igiene 43, fu 1mo", None),
    # the shape that USED to fail. the trigger was POSITION, not
    # multi-procedure-ness: measured 2026-09-05, three runs each, igiene
    # translated correctly as the FIRST procedure and came back raw as the
    # second, with or without an invoice clause. "igiene 11, comp 22" is
    # multi-procedure and always passed. closed by the 2026-09-06 retrain and
    # kept as the regression guard. mirrors notes_test.jsonl row 12.
    ("zzi_igiene_multi.txt",
     f"{CF_IGIENE_MULTI} Giulia Fontana, comp 20, igiene 43, paid 100 for comp 20, fu 3wk",
     None),
    (BAD_NOTE_NAME, "qwtpz nessun codice qui, solo rumore 8834 %%%", None),
]

RESULTS = []
# sessions from before the 2026-09-23 fix are not this run's to delete
RUN_STARTED = datetime.now(timezone.utc).isoformat()


def check(step, ok, note):
    RESULTS.append((step, bool(ok), note))
    print(f"  {'PASS' if ok else 'FAIL'}  {step}: {note}")
    return bool(ok)


# --- fixtures -------------------------------------------------------------


def seed(tmpdir):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO users (username, password_hash, role, active) VALUES (?, ?, ?, 1)",
        (STAFF_USER, generate_password_hash(STAFF_PASS), "dentist"),
    )
    conn.commit()
    conn.close()

    paths = []
    for name, body, _ in NOTES:
        p = Path(tmpdir) / name
        p.write_text(body)
        paths.append(str(p))
    return paths


def cleanup():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    marks = ",".join("?" * len(ALL_CFS))
    # POL-9: this walk's staged uploads - their folders first, then the rows.
    # the unreadable note has no codice fiscale, so it is found by uploader
    staged = conn.execute(
        f"SELECT staged_dir FROM note_reviews WHERE codice_fiscale IN ({marks})"
        f" OR created_by = ?", ALL_CFS + (STAFF_USER,)).fetchall()
    for row in staged:
        if row["staged_dir"] and "staging" in Path(row["staged_dir"]).parts:
            shutil.rmtree(row["staged_dir"], ignore_errors=True)
    # child-first. no try/except: a delete that cannot run is a cleanup that
    # did not happen and it should be loud.
    # child tables are keyed on the surrogate since Phase 51; `patients` is
    # still where the codice fiscale lives, so it is deleted by CF as before.
    pids = [r[0] for r in conn.execute(
        f"SELECT patient_id FROM patients WHERE codice_fiscale IN ({marks})", ALL_CFS)]
    if pids:
        pmarks = ",".join("?" * len(pids))
        # billing_* since P07: a filed note with an invoice line makes a ledger
        # invoice. this cleanup predated it and left one orphan row per run
        # (found 2026-09-23, 7 rows). child-first, like everything here.
        conn.execute(f"DELETE FROM billing_events WHERE patient_id IN ({pmarks})", pids)
        conn.execute(f"DELETE FROM billing_invoices WHERE patient_id IN ({pmarks})", pids)
        conn.execute(f"DELETE FROM visit_reviews WHERE visit_id IN"
                     f" (SELECT id FROM visits WHERE patient_id IN ({pmarks}))", pids)
        for table in ("invoices", "visits"):
            conn.execute(f"DELETE FROM {table} WHERE patient_id IN ({pmarks})", pids)
    conn.execute(f"DELETE FROM note_reviews WHERE codice_fiscale IN ({marks}) OR created_by = ?",
                 ALL_CFS + (STAFF_USER,))
    conn.execute(f"DELETE FROM patients WHERE codice_fiscale IN ({marks})", ALL_CFS)
    # the walk's own login session goes with its user - it was left behind
    # every run until 2026-09-23 (22 rows by then, recorded, not deleted)
    conn.execute("DELETE FROM sessions WHERE username = ? AND created_at >= ?",
                 (STAFF_USER, RUN_STARTED))
    conn.execute("DELETE FROM users WHERE username = ?", (STAFF_USER,))
    conn.commit()
    # the staff user is not the only key these rows carry: sort_files and the
    # sync audit under other actors with the patient in target. since P06 an
    # identity field holds the surrogate, not the cf, so both are matched. the
    # audit trail is append-only; purge_audit is the sanctioned way out, and
    # it leaves one row saying how many fixture rows it removed.
    keys = tuple(ALL_CFS) + tuple(pids)
    kmarks = ",".join("?" * len(keys))
    where = f"username = ? OR username IN ({kmarks}) OR target IN ({kmarks})"
    params = (STAFF_USER,) + keys + keys
    auth.purge_audit(conn, where, params, "e2e_intake_walk", "e2e fixture cleanup")
    left = conn.execute(
        f"SELECT COUNT(*) FROM patients WHERE codice_fiscale IN ({marks})", ALL_CFS
    ).fetchone()[0]
    left += conn.execute("SELECT COUNT(*) FROM sessions WHERE username = ? AND created_at >= ?",
                         (STAFF_USER, RUN_STARTED)).fetchone()[0]
    left += conn.execute(
        f"SELECT COUNT(*) FROM note_reviews WHERE codice_fiscale IN ({marks}) OR created_by = ?",
        ALL_CFS + (STAFF_USER,)).fetchone()[0]
    if pids:
        left += conn.execute(f"SELECT COUNT(*) FROM billing_invoices WHERE patient_id IN"
                             f" ({','.join('?' * len(pids))})", pids).fetchone()[0]
    audit_left = conn.execute(f"SELECT COUNT(*) FROM audit_log WHERE {where}", params).fetchone()[0]
    conn.close()

    # the search index too. a filed note is embedded into chroma with its cf in
    # the metadata, and this cleanup used to stop at sqlite and the files - so
    # every run left its patients' note text searchable after the patients
    # were gone (found by the P01 restore drill: 6 ZZI* chunks, 2026-09-10).
    # deleted by this walk's own cfs only, and counted, so check 9 bites.
    import storage
    collection = storage.get_collection("db/chroma")
    in_walk = {"codice_fiscale": {"$in": list(ALL_CFS)}}
    collection.delete(where=in_walk)
    index_left = len(collection.get(where=in_walk)["ids"])

    files_left = 0
    for cf in ALL_CFS:
        d = SORTED_ROOT / cf
        if d.exists():
            shutil.rmtree(d)
    nr = SORTED_ROOT / "needs_review"
    if nr.exists():
        for f in nr.iterdir():
            if f.name.startswith("zzi_"):
                f.unlink()
        files_left = len([f for f in nr.iterdir() if f.name.startswith("zzi_")])
    for cf in ALL_CFS:
        if (SORTED_ROOT / cf).exists():
            files_left += 1
    return left, files_left, audit_left, index_left


def procedures_for(cf, deadline):
    """The model's extraction for this note, as staged for review (POL-9)."""
    while time.time() < deadline:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT extraction FROM note_reviews WHERE codice_fiscale = ? AND origin = 'upload'"
            " AND extraction IS NOT NULL ORDER BY id DESC LIMIT 1",
            (cf,),
        ).fetchone()
        conn.close()
        if row:
            return json.loads(row["extraction"])["procedures"]
        time.sleep(0.25)
    return None


def walk_visits():
    conn = sqlite3.connect(DB_PATH)
    marks = ",".join("?" * len(ALL_CFS))
    n = conn.execute(f"SELECT COUNT(*) FROM visits v JOIN patients p ON p.patient_id ="
                     f" v.patient_id WHERE p.codice_fiscale IN ({marks})", ALL_CFS).fetchone()[0]
    conn.close()
    return n


def upload_audit_count(deadline, want):
    n = 0
    while time.time() < deadline:
        conn = sqlite3.connect(DB_PATH)
        n = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE username = ? AND action = 'upload_file'",
            (STAFF_USER,),
        ).fetchone()[0]
        conn.close()
        if n >= want:
            return n
        time.sleep(0.25)
    return n


def needs_review_row(deadline, name=BAD_NOTE_NAME):
    """Wait for THIS file to reach its terminal state, not for any file's.

    THE BUG THIS FIXES. It used to match any needs_review row for the user, and
    the walk uploads six notes. It returned as soon as the first one landed,
    which could be a different file - and the badge check then ran while THIS
    file's worker was still going, so its queue_upload row was the newest for
    its filename and /upload/recent correctly rendered `Queued`. The gate read
    as a badge bug; it was the test asking the wrong question.

    A file is terminal when the worker has written ITS OWN upload_file row. No
    fixed sleep: this polls the durable audit row the worker writes, and
    returns None on timeout so the caller fails loudly rather than racing on.
    """
    while time.time() < deadline:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM audit_log WHERE username = ? AND action = 'upload_file'"
            " AND target LIKE '%needs_review%' AND target LIKE ?"
            " ORDER BY id DESC LIMIT 1",
            (STAFF_USER, f"%{name}"),
        ).fetchone()
        conn.close()
        if row:
            return row
        time.sleep(0.25)
    return None


def worker_settled(deadline, name=BAD_NOTE_NAME):
    """True once no queue_upload row for `name` outranks its upload_file row.

    The fragment collapses to one row per filename, newest id wins. That is the
    contract the badge depends on, so this waits on exactly it rather than on a
    clock.
    """
    while time.time() < deadline:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        top = conn.execute(
            "SELECT action FROM audit_log WHERE username = ?"
            " AND action IN ('queue_upload', 'upload_file', 'sync_note')"
            " AND target LIKE ? ORDER BY id DESC LIMIT 1",
            (STAFF_USER, f"%{name}"),
        ).fetchone()
        conn.close()
        if top and top["action"] != "queue_upload":
            return True
        time.sleep(0.25)
    return False


def rows_for(name=BAD_NOTE_NAME):
    """Every intake row for one file, for a failure note that is worth reading."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, action, target FROM audit_log WHERE username = ?"
        " AND action IN ('queue_upload', 'upload_file', 'sync_note')"
        " AND target LIKE ? ORDER BY id", (STAFF_USER, f"%{name}%")).fetchall()
    conn.close()
    return [f"{r['id']}:{r['action']}:{r['target']}" for r in rows]


# --- the walk -------------------------------------------------------------


def sign_in(page):
    page.goto(f"{STAFF_URL}/login")
    page.fill('input[name="username"]', STAFF_USER)
    page.fill('input[name="password"]', STAFF_PASS)
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle")


def walk(browser, note_paths):
    page = browser.new_page()
    sign_in(page)
    check("1 staff login", "/login" not in page.url, f"landed on {page.url}")

    # scope to the UPLOAD form. a bare 'form button[type=submit]' matches the
    # logout button first and silently logs the walk out instead of uploading.
    upload_form = page.locator('form:has(input[name="files"])')
    submit = upload_form.locator('button[type="submit"]')

    page.set_input_files('input[name="files"]', note_paths)
    # app.js enables the button on the input's change event; if it never
    # enables, the files were not staged and there is nothing to submit
    page.wait_for_function(
        "() => { const b = document.querySelector('form:has(input[name=\"files\"]) button[type=submit]');"
        " return b && !b.disabled; }",
        timeout=10000,
    )
    submit.click()
    page.wait_for_load_state("networkidle")

    # assert the server actually took them, rather than trusting the click
    posted = upload_audit_count(time.time() + 60.0, len(note_paths))
    check(f"2 all {len(note_paths)} notes reached the server",
          posted == len(note_paths),
          f"{posted}/{len(note_paths)} upload_file audit rows")

    deadline = time.time() + 180.0

    for name, _, expected in NOTES[:4]:
        cf = {n: c for (n, _, _), c in zip(NOTES[:4], ALL_CFS[:4])}[name]
        actual = procedures_for(cf, deadline)
        check(f"3 {name} extracted",
              actual == expected,
              f"expected {expected}, got {actual}")

    # case 5: igiene in FIRST position. this always worked, so it was written
    # permissively while the defect was open - prophy or the raw term both
    # counted. the retrain of 2026-09-06 closed it, so the loose half is gone
    # and this now asserts the mapping outright.
    ig = procedures_for(CF_IGIENE, deadline)
    ig_l = [p.lower() for p in (ig or [])]
    check("4 igiene maps to prophy",
          ig is not None and any(p.startswith("prophy") for p in ig_l),
          f"got {ig}")

    # case 5b/5c: igiene in SECOND position - the shape that used to fail.
    # case 5 above puts it first, which always worked, so on its own it was
    # never igiene coverage.
    #
    # igiene was not alone: pulizia (also -> prophy) and panoramica (-> opg)
    # failed identically in second position, and all three map to a code the
    # italian word does not resemble. the terms that survived -
    # devitalizzazione -> rct, corona -> crown - had more later-position
    # training examples. that gap was closed by the rows added on 2026-09-05
    # and the retrain on 2026-09-06; validate_dataset check 4 keeps the
    # coverage from being lost again, and this is the end-to-end proof.
    igm = procedures_for(CF_IGIENE_MULTI, deadline)
    igm_l = [p.lower() for p in (igm or [])]

    # hard safety property: whatever happens to the igiene term, the OTHER
    # procedure must survive intact and no third treatment may appear
    check("5b multi-procedure note kept its other procedure",
          igm is not None and "comp 20" in igm_l and len(igm_l) == 2,
          f"got {igm}")

    # 5c was a PIN on the defect until 2026-09-06 - it asserted that igiene came
    # back raw, and failing was the success signal. the retrain fixed it
    # (procedures 0.88 -> 0.91, invoices 0.79 -> 0.97), so it is now an
    # ordinary assertion: the mapping must hold in second position, and the
    # untranslated term must not come back.
    check("5c igiene maps to prophy in second position",
          "prophy 43" in igm_l,
          f"got {igm}")
    check("5c the untranslated term is gone",
          not any(p.startswith("igiene") for p in igm_l),
          f"got {igm} - a raw italian term here is the pre-2026-09-06 defect returning")

    # POL-9: six notes read, none filed. nothing reaches visits before review
    check("5d nothing is filed before a dentist reviews it",
          walk_visits() == 0, f"{walk_visits()} visit row(s) for the walk's patients")

    # case 6: unreadable note -> needs_review -> phase 23's badge
    row = needs_review_row(deadline)
    check("5 unreadable note routed to needs_review",
          row is not None,
          f"audit target {row['target'] if row else None!r}")
    if row:
        check("6 needs_review carries a reason",
              row["reason"] == sort_files.REASON_EXTRACT_FAILED,
              f"reason {row['reason']!r}")

    # the fragment shows Queued until this file's own worker row lands. wait
    # for that exact transition - bounded, and loud if it never happens.
    settled = worker_settled(deadline)
    check("6b the worker reaches a terminal state for this file",
          settled,
          f"no non-queue audit row won for the failing note before the deadline. "
          f"rows seen: {rows_for()}")

    page.goto(f"{STAFF_URL}/upload/recent")
    body = page.content()
    check("7 Needs Review badge renders live",
          "Needs Review" in body,
          "badge present in /upload/recent")

    # scope to the failed note's own row. the other five notes legitimately
    # ARE Sorted, so a whole-fragment "Sorted" check would prove nothing -
    # this is the same whole-file-grep trap phase 22 hit.
    bad_row = ""
    for chunk in body.split("list-group-item"):
        if BAD_NOTE_NAME in chunk:
            bad_row = chunk
            break
    # report the badge ACTUALLY rendered, not just "OTHER": when this failed on
    # 2026-09-15 the note said OTHER and that told nobody anything.
    found_badge = re.search(r'<span class="badge[^"]*"[^>]*>([^<]+)</span>', bad_row)
    check("8 the failed note's own row is not Sorted",
          bad_row and "Needs Review" in bad_row and ">Sorted<" not in bad_row,
          f"row found={bool(bad_row)}, badge={found_badge.group(1).strip() if found_badge else None!r},"
          f" url={page.url}")
    rct_row = ""
    for chunk in body.split("list-group-item"):
        if "zzi_rct.txt" in chunk:
            rct_row = chunk
            break
    check("8b a readable note shows Awaiting review, not Sorted",
          "Awaiting review" in rct_row and ">Sorted<" not in rct_row,
          f"row found={bool(rct_row)}")

    # POL-9 end to end: the dentist opens the queue, confirms one note, and only
    # that note becomes a visit. the rest stay waiting.
    conn = sqlite3.connect(DB_PATH)
    rid = conn.execute("SELECT id FROM note_reviews WHERE codice_fiscale = ? AND status ="
                       " 'pending'", (CF_RCT,)).fetchone()
    failed_row = conn.execute("SELECT status FROM note_reviews WHERE created_by = ? AND"
                              " original_name = ?", (STAFF_USER, BAD_NOTE_NAME)).fetchone()
    conn.close()
    check("8c the unreadable note waits as could-not-be-read",
          failed_row is not None and failed_row[0] == "extraction_failed", f"{failed_row}")
    page.goto(f"{STAFF_URL}/reviews")
    check("10 the review queue lists the uploads", "Notes to review" in page.content(), page.url)
    if rid:
        page.goto(f"{STAFF_URL}/reviews/{rid[0]}")
        check("10b the original text is shown beside the extraction",
              "devitalizzazione dente 46" in page.content(), page.url)
        page.locator('form[action$="/confirm"] button[type="submit"]').click()
        page.wait_for_load_state("networkidle")
    check("11 one confirmed note becomes one visit", walk_visits() == 1,
          f"{walk_visits()} visit row(s) after confirming one note")
    page.close()


def main():
    from playwright.sync_api import sync_playwright

    try:
        urllib.request.urlopen(STAFF_URL, timeout=5)
    except Exception as exc:
        if not isinstance(exc, urllib.error.HTTPError):
            print(f"pre-flight: staff app not reachable at {STAFF_URL} - {exc}")
            return 2
    try:
        urllib.request.urlopen("http://localhost:11434/api/tags", timeout=5)
    except Exception as exc:
        print(f"pre-flight: ollama not reachable - {exc}")
        return 2

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        note_paths = seed(tmp)
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless="--headed" not in sys.argv)
                try:
                    walk(browser, note_paths)
                finally:
                    browser.close()
        finally:
            left, files_left, audit_left, index_left = cleanup()

    check("9 fixtures cleaned up",
          left == 0 and files_left == 0 and audit_left == 0 and index_left == 0,
          f"{left} row(s), {files_left} file(s), {audit_left} audit row(s), "
          f"{index_left} index chunk(s) left behind")

    failed = [s for s, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
