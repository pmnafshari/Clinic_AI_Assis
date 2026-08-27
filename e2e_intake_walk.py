"""staff intake walk - seven notes through the real upload path, unstubbed model.

closes FLOW-2: eval_notes.py scores the model against a jsonl file and the fast
suite stubs it entirely. neither has ever put a note through
upload -> drop -> worker -> sort_files -> dental-notes -> sqlite. this does.

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
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from werkzeug.security import generate_password_hash

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
    # known carried defect: igiene does not map to prophy. either outcome is
    # SAFE - what must never happen is it silently becoming a different
    # treatment. asserted as a membership test, not an equality one.
    ("zzi_igiene.txt", f"{CF_IGIENE} Davide Costa, igiene 43, fu 1mo", None),
    # the FAILING shape. igiene is context-dependent: it maps correctly alone
    # and fails alongside another procedure. mirrors notes_test.jsonl row 12.
    ("zzi_igiene_multi.txt",
     f"{CF_IGIENE_MULTI} Giulia Fontana, comp 20, igiene 43, paid 100 for comp 20, fu 3wk",
     None),
    (BAD_NOTE_NAME, "qwtpz nessun codice qui, solo rumore 8834 %%%", None),
]

RESULTS = []


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
    marks = ",".join("?" * len(ALL_CFS))
    # child-first. no try/except: a delete that cannot run is a cleanup that
    # did not happen and it should be loud.
    for table in ("invoices", "visits", "patients"):
        conn.execute(f"DELETE FROM {table} WHERE codice_fiscale IN ({marks})", ALL_CFS)
    conn.execute("DELETE FROM audit_log WHERE username = ?", (STAFF_USER,))
    # the staff user is not the only key these rows carry: sort_files and the
    # sync audit under other actors with the cf in target. delete on both or
    # the fixtures survive in the audit trail after the patients are gone.
    conn.execute(f"DELETE FROM audit_log WHERE username IN ({marks})", ALL_CFS)
    conn.execute(f"DELETE FROM audit_log WHERE target IN ({marks})", ALL_CFS)
    conn.execute("DELETE FROM users WHERE username = ?", (STAFF_USER,))
    conn.commit()
    left = conn.execute(
        f"SELECT COUNT(*) FROM patients WHERE codice_fiscale IN ({marks})", ALL_CFS
    ).fetchone()[0]
    audit_left = conn.execute(
        f"SELECT COUNT(*) FROM audit_log WHERE username = ?"
        f" OR username IN ({marks}) OR target IN ({marks})",
        (STAFF_USER,) + tuple(ALL_CFS) + tuple(ALL_CFS)
    ).fetchone()[0]
    conn.close()

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
    return left, files_left, audit_left


def procedures_for(cf, deadline):
    while time.time() < deadline:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT procedures FROM visits WHERE codice_fiscale = ? ORDER BY id DESC LIMIT 1",
            (cf,),
        ).fetchone()
        conn.close()
        if row:
            return json.loads(row["procedures"])
        time.sleep(0.25)
    return None


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


def needs_review_row(deadline):
    while time.time() < deadline:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM audit_log WHERE username = ? AND action = 'upload_file'"
            " AND target LIKE '%needs_review%' ORDER BY id DESC LIMIT 1",
            (STAFF_USER,),
        ).fetchone()
        conn.close()
        if row:
            return row
        time.sleep(0.25)
    return None


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

    # case 5: igiene. safe outcomes are the correct mapping OR the raw
    # flagged term. anything else means a treatment was silently changed.
    ig = procedures_for(CF_IGIENE, deadline)
    safe = ig is not None and any(
        p.lower().startswith("prophy") or p.lower().startswith("igiene") for p in ig
    )
    check("4 igiene did not become a different treatment",
          safe,
          f"got {ig} (prophy = fixed, igiene = known defect, anything else = unsafe)")

    # case 5b: the FAILING igiene shape. case 5 above uses a single-procedure
    # note, which the model gets right - so on its own it is not igiene
    # coverage. this is the multi-procedure form that actually reproduces.
    igm = procedures_for(CF_IGIENE_MULTI, deadline)
    igm_l = [p.lower() for p in (igm or [])]

    # hard safety property: whatever happens to the igiene term, the OTHER
    # procedure must survive intact and no third treatment may appear
    check("5b multi-procedure note kept its other procedure",
          igm is not None and "comp 20" in igm_l and len(igm_l) == 2,
          f"got {igm}")

    # characterisation: this PINS the known defect. it is expected to fail the
    # mapping. if this check fails, igiene may have been FIXED - verify against
    # notes_test.jsonl row 12 and update the records rather than assuming a
    # regression.
    reproduced = any(p.startswith("igiene") for p in igm_l)
    check("5c known igiene defect still reproduces (pinned)",
          reproduced,
          f"got {igm} - if this FAILS, igiene may be fixed; re-check eval row 12 "
          f"and update the docs rather than treating it as a regression")

    # case 6: unreadable note -> needs_review -> phase 23's badge
    row = needs_review_row(deadline)
    check("5 unreadable note routed to needs_review",
          row is not None,
          f"audit target {row['target'] if row else None!r}")
    if row:
        check("6 needs_review carries a reason",
              row["reason"] == sort_files.REASON_EXTRACT_FAILED,
              f"reason {row['reason']!r}")

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
    check("8 the failed note's own row is not Sorted",
          bad_row and "Needs Review" in bad_row and ">Sorted<" not in bad_row,
          f"row found={bool(bad_row)}, badge={'Needs Review' if 'Needs Review' in bad_row else 'OTHER'}")
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
            left, files_left, audit_left = cleanup()

    check("9 fixtures cleaned up",
          left == 0 and files_left == 0 and audit_left == 0,
          f"{left} row(s), {files_left} file(s), {audit_left} audit row(s) left behind")

    failed = [s for s, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
