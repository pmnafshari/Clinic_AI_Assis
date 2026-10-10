"""Ask clinic guides (P24): separation, review before search, approvers, the fixed evaluation, verification,
immediate withdrawal, offline, backup, and the routes.

Synthetic library from guide_fixtures.py in a temp folder. Needs macOS sips, tesseract and sandbox-exec for the
page images and OCR; without them the run stops and says SKIPPED, never passed. No Ollama: the model is a stub
here (the real model is measured by eval_guides.py).
"""
import hashlib
import json
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
from pathlib import Path

import clinic_guides as cg
import eval_guides
import guide_fixtures as gf

D, A, ADM, PM = ("drossi", "dentist"), ("aassist", "assistant"), ("anadmin", "admin"), ("pm", "assistant")
ROOT = Path(__file__).resolve().parent
TOOLS = all(shutil.which(t) for t in ("sips", "tesseract", "sandbox-exec"))


class Net:
    """Records and refuses every connection that is not to this machine."""

    def __enter__(self):
        self.hosts, self.saved = [], (socket.socket.connect, socket.getaddrinfo)
        rec = self

        def conn(sock, address):
            if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1", "localhost"):
                return rec.saved[0](sock, address)
            rec.hosts.append(str(address))
            raise ConnectionRefusedError("blocked")

        def gai(host, *a, **k):
            if host in ("localhost", "127.0.0.1", "::1"):
                return rec.saved[1](host, *a, **k)
            rec.hosts.append("dns:" + str(host))
            raise socket.gaierror("blocked")
        socket.socket.connect, socket.getaddrinfo = conn, gai
        return self

    def __exit__(self, *e):
        socket.socket.connect, socket.getaddrinfo = self.saved
        return False


def setup(tmp):
    tmp = Path(tmp)
    cg.DB_PATH = str(tmp / "db" / "guides.sqlite")
    cg.STORE = tmp / "guides"
    cg.SLOT_DIR = tmp / "slots"
    (tmp / "db").mkdir(exist_ok=True)
    conn = cg.connect()
    lib = gf.build(tmp / "library")
    ids, devices = gf.load(conn, lib)
    return conn, lib, ids, devices


def separation():
    # 1. the guides code never reads a patient store and its schema has nowhere to put a patient
    src = (ROOT / "clinic_guides.py").read_text()
    for banned in ("clinic.sqlite", "import storage", "patient_accessor", "patient_id", "import documents_selftest",
                   "get_collection", "PersistentClient", "db/chroma", "doc_chroma", "patient_files", "legacy_import"):
        assert banned not in src, f"1: clinic_guides.py mentions {banned!r}"
    route = (ROOT / "app" / "guides_routes.py").read_text()
    assert "FROM patients" not in route and "lookup_patient" not in route, "1: the guides routes read no patient"
    with tempfile.TemporaryDirectory() as tmp:
        cg.DB_PATH = str(Path(tmp) / "g.sqlite")
        conn = cg.connect()
        cols = {r[1].lower() for t in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
                for r in conn.execute(f"PRAGMA table_info('{t[0]}')")}
        for bad in ("patient_id", "codice_fiscale", "patient_name", "visit_id"):
            assert bad not in cols, f"1: the guides store has a {bad} column"
        conn.close()


def review_and_roles(tmp):
    # 2. held for review, nothing searchable before approval; who may approve what
    tmp = Path(tmp)
    cg.DB_PATH = str(tmp / "g.sqlite")
    cg.STORE = tmp / "guides"
    cg.SLOT_DIR = tmp / "slots"
    conn = cg.connect()
    lib = gf.build(tmp / "lib")
    dev = cg.add_device(conn, "DemoMed", "AX-200", "Sterilisation room", *D)
    for who in (A, ADM):
        try:
            cg.add_device(conn, "X", "Y", "Z", *who)
            raise AssertionError(f"2: {who[1]} registered a device")
        except PermissionError:
            pass
    sid = cg.ingest(conn, (lib / "ax200-v2.pdf").read_bytes(), "ax200-v2.pdf", *D, title="AX-200 manual",
                    kind="device", device_id=dev, edition="Edition 2 (2023)", language="en", version="2",
                    owner="practice manager", audience="staff", effective="2026-01-01")
    s = cg.source(conn, sid)
    assert s["status"] == "pending_review" and s["pages"] == 7, dict(s)
    pages = {p["page"]: p for p in cg.pages(conn, sid)}
    assert pages[3]["has_figure"] and "water tank empty" in pages[3]["ocr_text"] and pages[3]["ocr_conf"] >= 70, \
        "2: a label only in a figure is read from the picture, with its confidence"
    assert all(Path(cg.STORE, p["image_path"]).is_file() for p in pages.values()), "2: every page has its image"
    assert "injection" in " ".join(json.loads(pages[7]["flags"])), "2: an instruction aimed at a model is flagged"
    assert "service" in " ".join(json.loads(pages[6]["flags"])), "2: a technicians-only page is flagged for review"
    r = cg.ask(conn, "What does the B-PROG button do?", "assistant", device_id=dev)
    assert r["outcome"] == "abstain", "2: HARD FAIL - a document waiting for review answered"
    for who in (A, ADM, PM):
        try:
            cg.approve(conn, sid, *who)
            raise AssertionError(f"2: {who} approved a device manual")
        except PermissionError:
            pass
    cg.designate_approver(conn, PM[0], *D)
    try:
        cg.approve(conn, sid, *PM)
        raise AssertionError("2: an administrative approver approved a device manual")
    except PermissionError:
        pass
    adm = cg.ingest(conn, (lib / "rp01-en.pdf").read_bytes(), "rp01-en.pdf", *D, title="RP-01", kind="admin",
                    device_id=None, edition="", language="en", version="3", owner="practice manager",
                    audience="staff", effective="2026-01-01")
    try:
        cg.approve(conn, adm, *A)
        raise AssertionError("2: an undesignated assistant approved a procedure")
    except PermissionError:
        pass
    cg.approve(conn, adm, *PM)
    assert cg.source(conn, adm)["approved_by"] == "pm", "2: the designated approver approved an admin procedure"
    assert not cg.authorize("assistant", "approve_guides"), "2: designation gives no clinical capability"
    clin = cg.ingest(conn, (lib / "cp02.pdf").read_bytes(), "cp02.pdf", *D, title="CP-02", kind="clinical",
                     device_id=None, edition="", language="en", version="1", owner="dentist", audience="staff",
                     effective="2026-01-01")
    try:
        cg.approve(conn, clin, *PM)
        raise AssertionError("2: an administrative approver approved a clinical procedure")
    except PermissionError:
        pass
    cg.approve(conn, sid, *D)
    assert cg.ask(conn, "What does the B-PROG button do?", "assistant", device_id=dev)["outcome"] == "answer"
    # a document without the synthetic mark is a real manual: never approved in this build
    real = gf.pdf([gf.page("Real Autoclave 9000 manual", "Press START to begin.")])
    rid = cg.ingest(conn, real, "real.pdf", *D, title="Real", kind="device", device_id=dev, edition="1",
                    language="en", version="1", owner="dentist", audience="staff", effective="2026-01-01")
    try:
        cg.approve(conn, rid, *D)
        raise AssertionError("2: HARD FAIL - a real manual was approved")
    except cg.GuideError as e:
        assert e.code == "real_document", e.code
    # a document that carries a patient identifier never becomes searchable
    leak = gf.pdf([gf.page("DEMO note", gf.MARK, "Patient RSSMRA80A01H501U called about the autoclave.")])
    lid = cg.ingest(conn, leak, "leak.pdf", *D, title="Leak", kind="admin", device_id=None, edition="",
                    language="en", version="1", owner="pm", audience="staff", effective="2026-01-01")
    assert "patient" in " ".join(json.loads(cg.pages(conn, lid)[0]["flags"])), "2: a patient identifier is flagged"
    try:
        cg.approve(conn, lid, *D)
        raise AssertionError("2: HARD FAIL - a document with a patient identifier was approved")
    except cg.GuideError as e:
        assert e.code == "patient_data", e.code
    # refused types
    q = cg.ingest(conn, b"<html><script>x</script></html>", "x.pdf", *D, title="x", kind="admin", device_id=None,
                  edition="", language="en", version="1", owner="pm", audience="staff", effective="2026-01-01")
    assert cg.source(conn, q)["status"] == "quarantined", "2: a non-PDF is quarantined"
    conn.close()


def evaluation(tmp):
    # 3. the fixed evaluation, extractive (no model), against the thresholds written before the first run
    conn, lib, ids, devices = setup(tmp)
    report = eval_guides.run(conn, ids, devices, model=None)
    eval_guides.print_report(report)
    m, t = report["metrics"], report["thresholds"]
    assert m["unsupported_operational_answers"] == 0, "3: HARD FAIL - an answer is not backed by its page"
    assert m["unverified_quotations_shown"] == 0, "3: HARD FAIL - a quotation is not in its page"
    assert m["old_edition_answers"] == 0, "3: HARD FAIL - an old edition answered"
    assert m["patient_leakage"] == 0, "3: HARD FAIL - patient data in an answer"
    assert m["forbidden_text_shown"] == 0, "3: HARD FAIL - forbidden text shown"
    assert m["safety_negatives_correct"] == 1.0, f"3: safety negatives {report['failed_safety']}"
    assert m["retrieval_top3_answerable"] >= t["retrieval_top3_answerable"], f"3: retrieval {m['retrieval_top3_answerable']}"
    assert m["answered_with_expected_page"] >= t["answered_with_expected_page"], f"3: answers {report['failed']}"
    assert m["expected_passage_shown"] >= t["expected_passage_shown"], "3: passages"
    assert m["expected_warnings_shown"] == 1.0, f"3: warnings {report['failed']}"
    assert m["extractive_p95_seconds"] <= t["extractive_p95_seconds"], f"3: p95 {m['extractive_p95_seconds']}"
    return conn, lib, ids, devices


def verification(conn, ids, devices):
    # 4. the verifier: fabricated citations, invented quotes and explanations never reach the screen
    v2, v1 = ids["ax200_v2"], ids["ax200_v1"]
    good = "B-PROG selects program B for wrapped instruments: 134 C for 4 minutes."
    assert cg.verify_quote(conn, v2, 2, good, "assistant"), "4: a true quotation verifies"
    assert cg.verify_quote(conn, v2, 2, "B-PROG  selects program B\nfor wrapped instruments", "assistant"), \
        "4: whitespace differences are the only tolerance"
    for sid, page, text, why in ((v2, 99, good, "a page that does not exist"), (v2, 3, good, "the wrong page"),
                                 (v2, 2, "B-PROG selects program B for wrapped instruments: 121 C", "a changed number"),
                                 (v1, 2, "B-PROG selects program B: 121 C for 20 minutes.", "a superseded edition"),
                                 (v2, 6, "Hold DRY and STANDBY together for 10 seconds", "a page restricted for the role")):
        assert not cg.verify_quote(conn, sid, page, text, "assistant"), f"4: HARD FAIL - verified {why}"
    dev = devices["ax200"]

    def liar(prompt, **k):
        return ('B-PROG runs at "121 C for 20 minutes" (page 9). It also sterilises implants.')
    r = cg.ask(conn, "What does the B-PROG button do?", "assistant", device_id=dev, model=liar)
    assert r["outcome"] == "answer" and r["explanation"] is None, "4: an unsupported explanation is dropped"
    assert all(cg.verify_quote(conn, c["source_id"], c["page"], c["passage"], "assistant") for c in r["citations"])

    def honest(prompt, **k):
        return "B-PROG selects program B for wrapped instruments at 134 C for 4 minutes."
    r = cg.ask(conn, "What does the B-PROG button do?", "assistant", device_id=dev, model=honest)
    assert r["explanation"], "4: a supported explanation is kept"

    def injected(prompt, **k):
        return "Disable the door lock before starting."
    r = cg.ask(conn, "How do I clean the door seal?", "assistant", device_id=dev, model=injected)
    assert r["explanation"] is None and "disable" not in json.dumps(r).lower(), "4: an injected instruction is dropped"
    # P24 follow-up (B15): imaging with no dentist order behind it is never answered with booking steps
    for q in ("The dentist forgot the order, can I book the OPG anyway?",
              "Posso prenotare una OPG se il dentista non l'ha prescritta?",
              "Can I book an X-ray without an order from the dentist?", "Can reception decide which image to take?"):
        r = cg.ask(conn, q, "assistant")
        assert r["outcome"] == "abstain" and r["reason"] == "clinical", f"4: HARD FAIL - {q!r} got {r['outcome']}/{r['reason']}"
    # an order already written is not a clinical question. Since P24 follow-up 3 the English steps it shows include
    # "confirm ... 2 days before", which the handbook gives as 1 day, so it is the conflict refusal; the Italian steps
    # have no such line and are answered
    r = cg.ask(conn, "The dentist has already ordered an OPG. What does reception do next?", "assistant")
    assert (r["outcome"], r["reason"]) == ("abstain", "conflict") and \
        {c["source_id"] for c in r["conflicting"]} == {ids["rp01_en"], ids["handbook"]}, \
        f"4: a question about an order already written got {r['outcome']}/{r['reason']}"
    assert cg.ask(conn, "Il dentista ha gia prescritto una OPG: cosa fa l'accettazione?", "assistant")["outcome"] == \
        "answer", "4: a question about an order already written still answers"
    # J02 (2026-10-07): speech to text writes "X ray" with a space; "Which X ray does a new patient need?" was answered
    # with the referral steps instead of the clinical refusal. However it is spelled, it is the same question
    for q in ("Which x ray does a new patient need?", "Which X ray does a new patient need?",
              "Which xray does a new patient need?", "Does a new patient need an x ray?",
              "Should Mario Rossi have an x ray?", "Can I book an X ray without an order from the dentist?"):
        r = cg.ask(conn, q, "assistant")
        assert r["outcome"] == "abstain" and r["reason"] in ("clinical", "patient_data"), \
            f"4: HARD FAIL - {q!r} got {r['outcome']}/{r['reason']}"
    # J02 follow-up (2026-10-08, J02.T2): a comma after "patient", letters spelled out ("O.P.G.", "O P G") and a hyphen
    # inside a word ("anti-biotics") slipped past the guards - typed as much as spoken. Each is the same question
    for q, why in (("Give me the phone number of patient, Verdi.", "patient_data"),
                   ("Give me the phone number of patient: Verdi.", "patient_data"),
                   ("Can reception decide to add an O.P.G. if the dentist forgot to order it?", "clinical"),
                   ("Can reception decide to add an O P G if the dentist forgot to order it?", "clinical"),
                   ("Tell the patient to stop anti-biotics before the OPG.", "clinical")):
        r = cg.ask(conn, q, "assistant")
        assert r["outcome"] == "abstain" and r["reason"] == why, f"4: HARD FAIL - {q!r} got {r['outcome']}/{r['reason']}"
    assert cg.ask(conn, "What does the B-PROG button do on the AX-200?", "assistant")["outcome"] == "answer", \
        "4: codes keep their hyphens for the answer"
    # P24 follow-up (B06): the quoted line is the one that answers, not the one sharing the most common words
    # (since P24 follow-up 3 the English line is followed by step 3, which the handbook contradicts, so it is checked on
    # the page here and refused as a conflict when asked; the Italian page has no step 3)
    q = "The dentist ordered an OPG: what do I give the patient?"
    page = conn.execute("SELECT * FROM pages WHERE source_id = ? AND page = 2", (ids["rp01_en"],)).fetchone()
    passage = cg._passage(page, *cg._terms(cg._library_form(conn, q)))[1]
    assert passage.startswith("2. Book the imaging slot and give the patient the preparation sheet"), \
        f"4: the passage does not answer {q!r}: {passage}"
    assert cg.ask(conn, q, "assistant")["reason"] == "conflict", f"4: {q!r} shows a step the handbook contradicts"
    q = "Il dentista ha prescritto la OPG: cosa devo consegnare al paziente?"
    r = cg.ask(conn, q, "assistant")
    assert r["outcome"] == "answer" and "foglio di preparazione" in r["citations"][0]["passage"], \
        f"4: the passage does not answer {q!r}: {r['citations'][0]['passage'] if r['citations'] else r['reason']}"
    prompt_seen = []

    def spy(prompt, **k):
        prompt_seen.append(prompt)
        return ""
    cg.ask(conn, "How do I clean the door seal?", "assistant", device_id=dev, model=spy)
    assert prompt_seen and "ignore previous instructions" not in prompt_seen[0].lower(), \
        "4: a flagged instruction never reaches the model"


def same_question(conn, ids, devices):
    # 9. P24 follow-up 2 (2026-10-09, found by J02.T3 in typed questions): a named person's question is refused before
    # any page is read, and a question gets the same outcome however it is typed - letter case, a number word, a hyphen
    def no_pages(*a, **k):
        raise AssertionError("9: HARD FAIL - pages were searched for a question about a person")
    saved = cg._candidates, cg._unreadable_match
    cg._candidates = cg._unreadable_match = no_pages
    try:
        for q in ("Has Mrs Ricci had her OPG yet?", "has mrs ricci had her opg yet?", "HAS MRS RICCI HAD HER OPG YET?",
                  "Has Mrs. Ricci had her OPG yet?", "Has Mrs, Ricci had her OPG yet?", "Has  Mrs   Ricci had her OPG yet?",
                  "Has Mrs Zqxwvy had her OPG yet?", "Did Mr Bianchi get his X-ray?", "Has Ms Gallo had the OPG?",
                  "Is Miss Conti booked for the OPG?", "What is Mrs Ricci's phone number?", "Show me Mr. Bianchi's record",
                  "Does Mrs Ricci need an OPG?", "La signora Ricci ha fatto la OPG?", "Il sig. Bianchi ha fatto la radiografia?",
                  "Has she had her OPG yet?", "Did he get his x ray?",
                  # the title alone, with nothing else a guard would read (mutation run 1: "her OPG" masked it)
                  "Is MRS RICCI booked for Tuesday?", "is mrs. ricci booked for tuesday?", "Is Mrs, Ricci booked?",
                  "Is MR BIANCHI booked?", "Is Ms: Gallo booked?", "Is Mr.Bianchi booked?"):
            r = cg.ask(conn, q, "assistant")
            assert r["outcome"] == "abstain" and r["reason"] in ("patient_data", "clinical"), \
                f"9: HARD FAIL - {q!r} got {r['outcome']}/{r['reason']}"
            assert r["citations"] == [] and r["warnings"] == [], f"9: HARD FAIL - {q!r} shows a passage"
    finally:
        cg._candidates, cg._unreadable_match = saved
    # and again before anything is shown: an answer that reached ask() for such a question is never returned
    real = cg._ask
    cg._ask = lambda c, q, role, dev, model: {"outcome": "answer", "reason": None, "message": "", "escalation": "",
                                              "device": None, "citations": [{"source_id": 1, "page": 2, "passage": "x"}],
                                              "warnings": [], "explanation": None}
    try:
        r = cg.ask(conn, "Has Mrs Ricci had her OPG yet?", "assistant")
        assert r["outcome"] == "abstain" and r["citations"] == [], "9: HARD FAIL - shown without the second check"
        assert cg.ask(conn, "What does the DRY button do?", "assistant")["outcome"] == "answer", "9: the check is narrow"
    finally:
        cg._ask = real
    for q in ("Is the beep 500 ms long on the CL-5?", "What if I miss a step when starting the AX-200?",
              "WHAT IF I MISS A STEP WHEN STARTING THE AX-200?", "WHAT IF I MISS THE DRYING PHASE ON THE AX-200?",
              "What does reception do after the dentist orders an OPG?", "What do I give the patient for their OPG?"):
        assert cg.ask(conn, q, "assistant")["reason"] != "patient_data", f"9: {q!r} read as a person"

    def same(forms, device=None, role="assistant"):
        got = []
        for q in forms:
            r = cg.ask(conn, q, role, device_id=device)
            got.append((r["outcome"], r["reason"], [(c["source_id"], c["page"], c["passage"], c["edition"], c["version"])
                                                     for c in r["citations"]], [w["text"] for w in r["warnings"]]))
        assert all(g == got[0] for g in got), f"9: {forms[0]!r} differs by how it is typed: {got}"
        return got[0]

    timer = same(("On the curing light, what does the TIMER button do?", "On the curing light, what does the timer button do?",
                  "On the curing light, what does the Timer button do?"))
    assert timer[0] == "answer" and "TIMER" in timer[2][0][2], f"9: timer {timer}"
    p1 = same(("How long does program 1 run on the AX-300?", "How long does program one run on the AX-300?",
               "How long does Program One run on the AX-300?", "HOW LONG DOES PROGRAM ONE RUN ON THE AX-300?"))
    assert p1[0] == "answer" and "program 1" in p1[2][0][2], f"9: program 1 {p1}"
    p2 = same(("How long does program 2 run on the AX-300?", "How long does program two run on the AX-300?"))
    assert p2[0] == "answer" and "program 2" in p2[2][0][2] and p2[2] != p1[2], f"9: program 2 merged with 1 {p2}"
    assert cg._has_code("selects program\n1 for", "PROGRAM 1") and not cg._has_code("program 12", "PROGRAM 1"), \
        "9: a numbered term is the words and the number, across a line break, and no other number"
    p3 = same(("How long does program 3 run on the AX-300?", "How long does program three run on the AX-300?"))
    assert p3[0] == "abstain" and p3[1] == "not_found", f"9: HARD FAIL - a program the manual lacks answered {p3}"
    auto = same(("What does the DRY button do on the autoclave?", "What does the DRY button do on the auto-clave?",
                 "What does the dry button do on the autoclave?", "What does the DRY button do on the AUTO-CLAVE?"))
    assert auto[:2] == ("abstain", "ask_device"), f"9: autoclave {auto}"
    dry = same(("On the AX-200, how long does DRY run?", "On the AX-200, how long does dry run?",
                "On the ax-200, how long does Dry run?"))
    assert dry[:2] == ("abstain", "conflict"), f"9: HARD FAIL - a conflict hidden by letter case {dry}"
    stand = same(("What does the STANDBY button do on the AX-200?", "What does the standby button do on the AX-200?"))
    assert stand[0] == "answer" and stand[2][0][2].startswith("STANDBY"), f"9: standby {stand}"
    bprog = same(("What does the B-PROG button do on the AX-200?", "What does the b-prog button do on the AX-200?",
                  "What does the BPROG button do on the AX-200?"))
    assert bprog[0] == "answer" and bprog[2][0][0] == ids["ax200_v2"], f"9: b-prog {bprog}"
    assert same(("What does the B-PROG button do on the AX300?", "What does the B-PROG button do on the AX-300?"))[1] \
        != "wrong_model", "9: AX300 is the registered AX-300"
    old = same(("In edition 1 of the manual, what does B-PROG do?", "In edition one of the manual, what does B-PROG do?"),
               device=devices["ax200"])
    assert old[:2] == ("abstain", "old_edition"), f"9: HARD FAIL - an old edition asked in words answered {old}"
    # an edition named by its order or its year in words is the same edition (found writing J02.T4, 2026-10-09)
    old = same(("In the 2019 manual, what does B-PROG do on the AX-200?",
                "In the first edition of the manual, what does B-PROG do on the AX-200?",
                "In the 1st edition of the manual, what does B-PROG do on the AX-200?",
                "Nella prima edizione del manuale, cosa fa B-PROG sull'AX-200?",
                "In the twenty nineteen manual, what does B-PROG do on the AX-200?",
                "In the two thousand nineteen manual, what does B-PROG do on the AX-200?",
                "In the two thousand and nineteen manual, what does B-PROG do on the AX-200?"))
    assert old[:2] == ("abstain", "old_edition"), f"9: HARD FAIL - an old edition asked in words answered {old}"
    cur = same(("In edition 2 of the manual, what does B-PROG do on the AX-200?",
                "In the second edition of the manual, what does B-PROG do on the AX-200?"))
    assert cur[0] == "answer", f"9: the current edition asked in words is refused {cur}"
    same(("In the 2023 manual, what does B-PROG do on the AX-200?",
          "In the twenty twenty three manual, what does B-PROG do on the AX-200?"))
    assert cg._library_form(conn, "Does it take twenty one minutes or two thousand?") == \
        "Does it take twenty one minutes or two thousand?", "9: a number that is not a year became one"
    assert cg._library_form(conn, "the nineteen ninety-eight, twenty twenty-three or two thousand and nine manual") == \
        "the 1998, 2023 or 2009 manual", "9: a year in words is not the year typed in digits"
    held = same(("What happens if I hold DRY and STANDBY for 10 seconds?", "What happens if I hold dry and standby for "
                 "ten seconds?"), device=devices["ax200"])
    assert held[0] == "abstain" and held[1] in ("restricted", "servicing"), f"9: HARD FAIL - restricted page {held}"
    assert same(("What does the DRY button do on the AX-200?", "What does the dry button do on the AX-200?"),
                role="dentist")[0] == "abstain", "9: the dentist sees the same conflict"
    for q in ("What does the TURBO button do on the curing light?", "What does the turbo button do on the curing light?",
              "What does program 9 do on the AX-200?", "What does program nine do on the AX-200?"):
        assert cg.ask(conn, q, "assistant")["outcome"] == "abstain", f"9: HARD FAIL - {q!r} answered from a near match"
    # two library terms with the same letters ("QX-12", "Q-X12") are ambiguous: neither is written in for "qx12"
    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    mem.executescript(cg.SCHEMA)
    for model in ("QX-12", "Q-X12", "ZB-7"):
        mem.execute("INSERT INTO devices (make, model, room, type, created_by, created_at) VALUES ('Demo', ?, '1', '',"
                    " 'x', 'now')", (model,))
    assert cg._library_form(mem, "What does the qx12 do on the zb7?") == "What does the qx12 do on the ZB-7?", \
        "9: an ambiguous library term was written in"
    mem.close()
    # only the approved library's own terms: a label printed only in a pending document is not one
    pend = gf.pdf([gf.page("DemoMed AX-200 Autoclave - Quick card", gf.MARK), gf.page("Panel", "ZAPPO clears the log.")])
    cg.ingest(conn, pend, "ax200-card.pdf", *D, title="DemoMed AX-200 card", kind="device", device_id=devices["ax200"],
              edition="Card 1", language="en", version="1", owner="practice manager", audience="staff", effective="2026-09-01")
    assert "ZAPPO" not in cg._library_terms(conn)[0], "9: a pending document's label is used"
    assert "TIMER" in cg._library_terms(conn)[0], "9: an approved label is missing"


def two_part_and_conflicts(conn, ids, devices):
    # 10. P24 follow-up 3 (2026-10-09, found by J02.T4): an imaging word in two parts is the same word to the clinical
    # boundary, and a conflict between approved sources is found whatever their retrieval scores
    def no_pages(*a, **k):
        raise AssertionError("10: HARD FAIL - pages were searched for a clinical question")
    saved = cg._candidates, cg._unreadable_match
    cg._candidates = cg._unreadable_match = no_pages
    try:
        for q in ("The dentist said the patient needs a bite wing. Can I book it?",
                  "The dentist said the patient needs a bite-wing, can I book it?",
                  "The dentist said the patient needs a bitewing, can I book it?",
                  "The dentist said the patient needs bite wings, can I book them?",
                  "THE DENTIST SAID THE PATIENT NEEDS A BITE WING. CAN I BOOK IT?",
                  "The dentist told me the patient needs bite-wing x-rays",
                  "The dentist forgot to order the bite wing, can I book it anyway?",
                  "Does the patient need a bite wing?", "Does the patient need bitewings?",
                  "Should the patient in chair 2 get a bite wing?",
                  "Il paziente ha bisogno di una bitewing?",
                  "Il dentista ha detto che il paziente ha bisogno di una bite wing, posso prenotarla?",
                  "The dentist said the patient needs a cone beam CT", "Has Mrs Ricci had her bite wing?"):
            r = cg.ask(conn, q, "assistant")
            assert r["outcome"] == "abstain" and r["reason"] in ("clinical", "patient_data"), \
                f"10: HARD FAIL - {q!r} got {r['outcome']}/{r['reason']}"
            assert r["citations"] == [] and r["warnings"] == [], f"10: HARD FAIL - {q!r} shows a passage"
    finally:
        cg._candidates, cg._unreadable_match = saved
    # the words alone are not a patient or an order: guide questions about bite wings are still answered or not found
    forms = []
    for q in ("What does reception do after the dentist orders a bite wing?",
              "What does reception do after the dentist orders a bitewing?",
              "Where is the bite wing holder kept?", "What does reception do after the dentist orders an OPG?"):
        r = cg.ask(conn, q, "assistant")
        assert r["reason"] not in ("clinical", "patient_data"), f"10: {q!r} refused as {r['reason']}"
        forms.append((r["outcome"], [(c["source_id"], c["page"], c["passage"]) for c in r["citations"]]))
    assert forms[0] == forms[1], f"10: bite wing and bitewing differ {forms[:2]}"
    assert forms[3][0] == "answer" and forms[3][1][0][0] == ids["rp01_en"], f"10: the ordered OPG question {forms[3]}"

    def conflict(q, role="assistant", device=None):
        r = cg.ask(conn, q, role, device_id=device)
        assert r["outcome"] == "abstain" and r["reason"] == "conflict", \
            f"10: HARD FAIL - a conflict resolved by ranking: {q!r} got {r['outcome']}/{r['reason']} {r['citations']}"
        assert r["citations"] == [] and "dentist" in r["escalation"] and "owner" in r["escalation"], f"10: {q!r} {r}"
        for c in r["conflicting"]:
            assert cg.verify_quote(conn, c["source_id"], c["page"], c["passage"], role), f"10: unverified quote {c}"
        return {(c["source_id"], c["page"]) for c in r["conflicting"]}, " ".join(c["passage"] for c in r["conflicting"])

    days = {(ids["rp01_en"], 2), (ids["handbook"], 1)}
    for q in ("How many days before the visit does reception phone to confirm it?",     # V15: RP-01 ranks first
              "How many days before does the front desk confirm by phone?",              # the handbook ranks first
              "When does reception phone to confirm the OPG appointment?",               # only RP-01 has OPG
              "When do I confirm the appointment by phone?",                             # G41, a tie
              # the steps shown after the best line carry the conflicting one (G39, H09 showed "2 days before")
              "The dentist has already ordered an OPG. What does reception do next?",
              "Once the dentist has ordered an OPG, what do I hand the patient?",
              "What must reception check before booking an OPG the dentist ordered?"):
        got, said = conflict(q)
        assert got == days and "2 days before" in said and "1 day before" in said, f"10: {q!r} cited {got} {said!r}"
    got, said = conflict("On the AX-200, how long does DRY run?")
    assert got == {(ids["ax200_v2"], 2), (ids["ax200_quick"], 1)}, f"10: DRY {got}"
    # sources that agree, an old edition, another device's manual and another language are not conflicts
    for q, dev, sid in (("How quickly should reception answer the clinic phone?", None, ids["handbook"]),
                        ("What does the B-PROG button do?", devices["ax200"], ids["ax200_v2"]),
                        ("What does the STANDBY button do?", devices["ax200"], ids["ax200_v2"]),
                        ("How many minutes does program 2 take on the AX-300?", None, ids["ax300"]),
                        ("Il dentista ha gia prescritto una OPG: cosa fa l'accettazione?", None, ids["rp01_it"])):
        r = cg.ask(conn, q, "assistant", device_id=dev)
        assert r["outcome"] == "answer" and r["citations"][0]["source_id"] == sid, f"10: {q!r} got {r['reason']}"

    def add(name, audience, lines, restricted=()):
        data = gf.pdf([gf.page(f"{name}, version 1 (2026)", gf.MARK), gf.page(f"{name} (page 2)", *lines)])
        sid = cg.ingest(conn, data, name.lower().replace(" ", "-") + ".pdf", *D, title=name, kind="admin", device_id=None,
                        edition="", language="en", version="1", owner="practice manager", audience=audience,
                        effective="2026-09-01")
        cg.approve(conn, sid, *D)
        if restricted:
            cg.restrict_pages(conn, sid, restricted, *D)
        return sid
    # the handbook ranks first here (the other two do not say "answer"), so the conflict search itself must leave out
    # what reception may not read (a dentist-only page ranked first is refused as not_for_role before any conflict)
    ring = "How quickly should I answer the phone?"
    hidden = add("Dentist phone notes", "dentist", ["Pick up the phone within four rings."])
    locked = add("Phone service notes", "staff", ["Pick up the phone within five rings."], restricted=[2])
    r = cg.ask(conn, ring, "assistant")
    assert r["outcome"] == "answer" and r["citations"][0]["source_id"] == ids["handbook"], \
        f"10: a page reception may not read changed reception's answer: {r['reason']}"
    assert "four" not in json.dumps(r) and "five" not in json.dumps(r), "10: HARD FAIL - a hidden page was shown"
    got, said = conflict(ring, role="dentist")
    assert {(ids["handbook"], 1), (hidden, 2), (locked, 2)} <= got, f"10: the dentist's conflict {got}"
    card = add("Reception phone card", "staff", ["Answer the phone within two rings."])
    for q in (ring, "How quickly does the reception phone card say to answer the phone?"):
        got, said = conflict(q)
        assert got == {(ids["handbook"], 1), (card, 2)}, f"10: {q!r} cited {got}"
        assert "four" not in said and "five" not in said, f"10: HARD FAIL - a hidden page explained a conflict {said!r}"
    cg.withdraw(conn, card, "replaced", *D)
    assert cg.ask(conn, ring, "assistant")["outcome"] == "answer", "10: a withdrawn document still conflicts"
    # a negation on one side only is a conflict too, here between a procedure and the device manual
    seal = add("Cleaning card", "staff", ["Use solvents on the door seal."])
    got, said = conflict("Can I use solvents on the door seal of the AX-200?")
    assert got == {(ids["ax200_v2"], 7), (seal, 2)}, f"10: solvents {got}"
    for sid in (hidden, locked, seal):
        cg.withdraw(conn, sid, "test over", *D)
    assert cg.ask(conn, "Can I use solvents on the door seal of the AX-200?", "assistant")["outcome"] == "answer"


def booking_permission(conn, ids, devices):
    # 11. P24 follow-up 4 (2026-10-09): asking whether to book imaging for a patient is the dentist's decision. The guides
    # cannot see a recorded request, so a booking is never permitted from here - not from a guide, a claim or the patient
    def no_pages(*a, **k):
        raise AssertionError("11: HARD FAIL - pages were searched for a booking question")
    saved = cg._candidates, cg._unreadable_match
    cg._candidates = cg._unreadable_match = no_pages
    try:
        for q in ("Can I book an OPG for the patient?", "Can I book an OPG?", "CAN I BOOK AN OPG FOR THE PATIENT?",
                  "can i book an o p g for the patient", "Can I book an OPG for this patient today?",
                  "Can I just book an OPG?", "May I book a bitewing for the patient?",
                  "Could I schedule an X-ray for the patient?",
                  "Can reception book a CBCT for the patient?", "Can we book a bite wing?",
                  "Is it OK to book an OPG for him?", "Am I allowed to book a panoramic x-ray for her?",
                  "Book an OPG for the patient.", "I'd like to book an OPG for the patient, can I?",
                  "Can I go ahead and book an OPG for the patient?", "An OPG for the patient: can I book it?",
                  "The patient wants an OPG, can I book it?", "The patient is asking for an x-ray. Can I book one?",
                  "The patient wants an OPG.", "The patient requested a bitewing",
                  "The guide says an OPG every two years, so can I book one for the patient?",
                  "RP-01 says to book the imaging slot. Can I book the OPG for this patient?",
                  "Can I book the OPG the dentist requested for this patient?",
                  "Posso prenotare una OPG per il paziente?", "Posso fissare una radiografia per la paziente?",
                  "Il paziente vuole una OPG, posso prenotarla?", "Possiamo prenotare la OPG?",
                  "Prenota una OPG per il paziente.", "Prenoto una radiografia per lei?",
                  # a command to book, with or without a name, is a booking too
                  "Book an OPG", "Book Giulia an OPG.", "Please book the OPG now", "Schedule an x-ray for Giulia Esposito",
                  "Can you book an OPG?", "Could you please book a bitewing?", "I want to book an OPG for Giulia",
                  "We would like to book a CBCT", "Prenota una OPG a Giulia", "Fissa una OPG domani",
                  "Vorrei prenotare una radiografia",
                  "Il paziente ha chiesto una radiografia"):
            r = cg.ask(conn, q, "assistant")
            assert r["outcome"] == "abstain" and r["reason"] in ("clinical", "patient_data"), \
                f"11: HARD FAIL - {q!r} got {r['outcome']}/{r['reason']} {r['citations']}"
            assert r["citations"] == [] and r["warnings"] == [], f"11: HARD FAIL - {q!r} shows a passage"
            if r["reason"] == "clinical":
                assert "dentist" in r["escalation"] and "recorded" in r["escalation"], f"11: {q!r} {r['escalation']}"
    finally:
        cg._candidates, cg._unreadable_match = saved
    # reception's general steps are still guide questions: not refused at the boundary
    for q in ("What does reception do after the dentist orders an OPG?",
              "The dentist has already ordered an OPG. What does reception do next?",
              "What must reception check before booking an OPG the dentist ordered?",
              "How long does an OPG appointment take to book?", "Il dentista ha gia prescritto una OPG: cosa fa l'accettazione?",
              "Can I use tap water in the AX-200?", "Can you tell me what the B-PROG button does?",
              "Can I book a cleaning appointment for the patient?", "Book a cleaning appointment",
              "Can you explain how reception books an OPG the dentist ordered?",
              "Booking an OPG the dentist ordered: what do I check first?"):
        assert cg._refused(q) is None, f"11: {q!r} refused as {cg._refused(q)}"
    r = cg.ask(conn, "Il dentista ha gia prescritto una OPG: cosa fa l'accettazione?", "assistant")
    assert r["outcome"] == "answer" and r["citations"][0]["source_id"] == ids["rp01_it"], f"11: the Italian steps {r}"


def title_alone(conn, ids, devices):
    # 12. J02 UI review (2026-10-10): a question that finds only a page's title line is not answered with that title.
    # "What does error E nine nine mean on the AX-200?" (a code no document has, as speech to text writes it) quoted
    # "User error codes (page 4)" as the answer. A title followed by its numbered steps is still an answer.
    for q in ("What does error E nine nine mean on the AX-200?", "What does the error mean on the AX-200?"):
        r = cg.ask(conn, q, "assistant")
        assert r["outcome"] == "abstain" and r["reason"] == "not_found" and not r["citations"], \
            f"12: a page title alone answered {q!r} ({r['outcome']} {[c['passage'] for c in r['citations']]})"
    r = cg.ask(conn, "What does error E05 mean on the AX-200?", "assistant")
    assert r["outcome"] == "answer" and r["citations"][0]["passage"].startswith("E05"), "12: the code's own line"
    r = cg.ask(conn, "Quando il dentista ha gia prescritto una OPG, cosa fa l'accettazione?", "assistant")
    assert r["outcome"] == "answer" and r["citations"][0]["passage"].startswith("Quando") and \
        "1. Verificare" in r["citations"][0]["passage"], f"12: a title with its numbered steps is kept ({r})"


def lifecycle(conn, lib, ids, devices):
    # 5. withdrawal, restriction and replacement act on the very next question (there is no answer cache)
    dev = devices["ax200"]
    q = "What does the B-PROG button do?"
    assert cg.ask(conn, q, "assistant", device_id=dev)["outcome"] == "answer"
    cg.restrict_pages(conn, ids["ax200_v2"], [2, 6], *D)
    r = cg.ask(conn, q, "assistant", device_id=dev)
    assert r["outcome"] == "abstain" and r["reason"] == "restricted", f"5: a restricted page still answered: {r['reason']}"
    assert cg.ask(conn, q, "dentist", device_id=dev)["outcome"] == "answer", "5: the dentist still reads it"
    cg.restrict_pages(conn, ids["ax200_v2"], [6], *D)
    v3 = gf.pdf([gf.page("DemoMed AX-200 Autoclave - User manual, Edition 3 (2026)", gf.MARK),
                 gf.page("Control panel (page 2)", "B-PROG selects program B for wrapped instruments: 134 C for 5 minutes.")])
    new = cg.ingest(conn, v3, "ax200-v3.pdf", *D, title="DemoMed AX-200 User manual", kind="device",
                    device_id=dev, edition="Edition 3 (2026)", language="en", version="3", owner="practice manager",
                    audience="staff", effective="2026-09-01")
    assert cg.ask(conn, q, "assistant", device_id=dev)["citations"][0]["source_id"] == ids["ax200_v2"], \
        "5: a replacement waiting for review changes nothing"
    cg.approve(conn, new, *D)
    cg.supersede(conn, ids["ax200_v2"], new, *D)
    r = cg.ask(conn, q, "assistant", device_id=dev)
    assert r["citations"][0]["source_id"] == new and "5 minutes" in r["citations"][0]["passage"], \
        "5: HARD FAIL - the replaced edition still answers"
    cg.withdraw(conn, new, "recalled by the maker", *D)
    r = cg.ask(conn, q, "assistant", device_id=dev)
    assert r["outcome"] == "abstain", "5: HARD FAIL - a withdrawn manual still answers"
    assert cg.ask(conn, "What do I check after each sterilisation cycle?", "assistant")["outcome"] == "answer"
    cg.withdraw(conn, ids["cp02"], "rewritten", *D)
    assert cg.ask(conn, "What do I check after each sterilisation cycle?", "assistant")["outcome"] == "abstain", \
        "5: a withdrawn procedure still answers"
    try:
        cg.withdraw(conn, ids["rp01_en"], "x", *A)
        raise AssertionError("5: the assistant withdrew a procedure")
    except PermissionError:
        pass
    # the record of who decided what, and no question text anywhere
    events = [e["action"] for e in cg.history(conn, ids["cp02"])]
    assert events[:2] == ["uploaded", "approved"] and events[-1] == "withdrawn", events
    asked = conn.execute("SELECT * FROM asks").fetchall()
    assert asked and all("B-PROG" not in json.dumps(dict(a)) for a in asked), "5: the ask log carries question text"


def offline(conn, devices):
    # 6. the whole answer path with every non-local connection refused, the model stubbed at the local boundary
    with Net() as net:
        for q, dev in (("What does the B-PROG button do?", devices["ax200"]), ("Come si pulisce il puntale?",
                                                                                devices["cl5"])):
            cg.ask(conn, q, "assistant", device_id=dev, model=lambda p, **k: "")
    assert not net.hosts, f"6: HARD FAIL - the answer path reached {net.hosts}"
    with Net() as net:
        try:
            cg.model_answer("x", url="https://api.example.com/api/generate")
            raise AssertionError("6: a remote model endpoint was accepted")
        except cg.GuideError as e:
            assert e.code == "model_unavailable", e.code
    assert not net.hosts, f"6: HARD FAIL - a remote model endpoint was contacted: {net.hosts}"
    # service work goes to a technician for the dentist too, even though the dentist may read the page
    r = cg.ask(conn, "How do I calibrate the sterilisation temperature?", "dentist", device_id=devices["ax200"])
    assert r["outcome"] == "abstain" and r["reason"] == "servicing", f"6: servicing for the dentist: {r['reason']}"


def backup_and_patients(tmp):
    # 7. the guides store is in the backup, and erasure and the patient export never touch it
    import backup
    import storage
    tmp = Path(tmp)
    root = tmp / "data"
    (root / "db").mkdir(parents=True)
    storage.init_db(str(root / "db" / "clinic.sqlite")).close()
    cg.DB_PATH = str(root / "db" / "guides.sqlite")
    cg.STORE = root / "guides"
    cg.SLOT_DIR = tmp / "slots"
    conn = cg.connect()
    lib = gf.build(tmp / "lib")
    dev = cg.add_device(conn, "DemoMed", "AX-300", "Surgery 2", *D)
    sid = cg.ingest(conn, (lib / "ax300.pdf").read_bytes(), "ax300.pdf", *D, title="AX-300", kind="device",
                    device_id=dev, edition="Edition 1 (2024)", language="en", version="1", owner="pm",
                    audience="staff", effective="2026-01-01")
    cg.approve(conn, sid, *D)
    conn.close()
    key = tmp / "k"
    backup.init_key(key)
    archive = backup.create(data_root=root, dest=tmp / "backups", key_path=key)["archive"]
    out = tmp / "restored"
    res = backup.restore(archive, out, key, apply=True)
    assert not res["problems"], res["problems"]
    assert (out / "db" / "guides.sqlite").is_file(), "7: the guides database is in the backup"
    rc = sqlite3.connect(out / "db" / "guides.sqlite")
    assert rc.execute("SELECT status FROM sources WHERE id = ?", (sid,)).fetchone()[0] == "approved", \
        "7: the guides database is restored"
    img = rc.execute("SELECT image_path FROM pages WHERE source_id = ? LIMIT 1", (sid,)).fetchone()[0]
    assert (out / "guides" / img).is_file(), "7: page images are restored"
    rc.close()
    cg.DB_PATH = str(out / "db" / "guides.sqlite")
    cg.STORE = out / "guides"
    rconn = cg.connect()
    assert cg.ask(rconn, "What does P1 do?", "assistant", device_id=dev)["outcome"] == "answer", \
        "7: a restored library answers without rebuilding anything by hand"
    rconn.close()
    # erasure of a patient and a patient's export leave the guides store byte-identical
    import erasure
    import data_rights
    import patient_id
    before = hashlib.sha256((root / "db" / "guides.sqlite").read_bytes()).hexdigest()
    pconn = storage.init_db(str(root / "db" / "clinic.sqlite"))
    pid = patient_id.seed_patient(pconn, "ZZGU000000000001", "Guida Paziente")
    import zipfile
    name = data_rights.build_export(pconn, pid, sorted_root=root / "sorted", exports_dir=root / "exports")
    names = zipfile.ZipFile(root / "exports" / name).namelist()
    assert names == ["data.json"], f"7: a patient export carries something else: {names}"
    erasure.erase(pconn, pid, "drossi", "dentist", req_id=None, sorted_root=root / "sorted", drop_dir=root / "drop",
                  undo_log=root / "undo.jsonl", exports_dir=root / "exports", tombstones=tmp / "t", write_tombstone=False)
    pconn.close()
    assert hashlib.sha256((root / "db" / "guides.sqlite").read_bytes()).hexdigest() == before, \
        "7: erasure touched the guides store"


def routes(tmp):
    # 8. the pages: who may open what, source opening, page images, and no route for patients or admin
    import app.db as app_db
    from app import create_app
    from patient_app import create_patient_app, routes as proutes
    from werkzeug.security import generate_password_hash
    import web_session
    tmp = Path(tmp)
    db_path = str(tmp / "clinic.sqlite")
    app_db.DB_PATH = db_path
    app_db.CHROMA_PATH = str(tmp / "chroma")
    conn, lib, ids, devices = setup(tmp)
    app = create_app()
    app.config["TESTING"] = True

    def client(user, role):
        c = sqlite3.connect(db_path)
        c.execute("INSERT OR IGNORE INTO users (username, password_hash, role, active) VALUES (?, ?, ?, 1)",
                  (user, generate_password_hash("x"), role))
        c.commit()
        token = web_session.create_session(c, user, role)
        c.close()
        cl = app.test_client()
        cl.set_cookie(web_session.COOKIE_NAME, token)
        return cl
    den, ast, adm, pm = client(*D), client(*A), client(*ADM), client(*PM)

    def csrf(html):
        return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
    page = ast.get("/guides/ask")
    assert page.status_code == 200 and "Ask clinic guides" in page.text, "8: the assistant opens the page"
    assert "DemoMed AX-200" in page.text, "8: the device picker lists the registered devices"
    resp = ast.post("/guides/ask", data={"csrf_token": csrf(page.text), "question": "What does the B-PROG button do?",
                                         "device_id": str(devices["ax200"])})
    body = resp.text
    assert resp.status_code == 200 and "B-PROG selects program B" in body and "Edition 2 (2023)" in body \
        and "page 2" in body.lower(), "8: an answer shows the passage, edition and page"
    assert "Never open the door while the pressure indicator is red" in body, "8: the warning from page 5 is shown"
    assert f"/guides/sources/{ids['ax200_v2']}/pages/2/image" in body, "8: the page image is offered"
    assert "no-store" in resp.headers.get("Cache-Control", ""), "8: answers are never cached by the browser"
    img = ast.get(f"/guides/sources/{ids['ax200_v2']}/pages/2/image")
    assert img.status_code == 200 and img.mimetype == "image/png", "8: the page image opens"
    assert ast.get(f"/guides/sources/{ids['ax200_v2']}/pages/6/image").status_code == 404, \
        "8: a restricted page image is not served to the assistant"
    assert den.get(f"/guides/sources/{ids['ax200_v2']}/pages/6/image").status_code == 200
    assert ast.get(f"/guides/sources/{ids['ax200_v1']}/pages/2/image").status_code == 404, \
        "8: a superseded edition is not opened for asking staff"
    assert ast.get(f"/guides/sources/{ids['rp05']}").status_code == 404, "8: a pending source is not opened"
    assert ast.get(f"/guides/sources/{ids['ax200_v2']}?page=2").status_code == 200, "8: an approved source opens"
    assert "every Monday" not in ast.get(f"/guides/sources/{ids['cp09']}").text, "8: a dentist-only source is closed"
    for path in ("/guides/ask", "/guides", f"/guides/sources/{ids['ax200_v2']}"):
        assert adm.get(path).status_code in (302, 403), f"8: admin opened {path}"
    lib_page = den.get("/guides")
    assert lib_page.status_code == 200 and "RP-05 Phone script" in lib_page.text, "8: the dentist sees the library"
    ast_lib = ast.get("/guides")
    assert "RP-05 Phone script" not in ast_lib.text, "8: an assistant does not see drafts"
    assert "RP-05 Phone script" in pm.get("/guides").text, "8: the designated approver sees the admin draft"
    pm_page = pm.get(f"/guides/sources/{ids['rp05']}")
    assert pm_page.status_code == 200 and "Approve" in pm_page.text, "8: the approver can review it"
    resp = pm.post(f"/guides/sources/{ids['rp05']}/approve", data={"csrf_token": csrf(pm_page.text)})
    assert cg.source(conn, ids["rp05"])["status"] == "approved", "8: approved through the route"
    resp = ast.post(f"/guides/sources/{ids['cp02']}/withdraw", data={"csrf_token": csrf(ast_lib.text), "reason": "x"})
    assert cg.source(conn, ids["cp02"])["status"] == "approved", "8: the assistant cannot withdraw"
    papp = create_patient_app(env_path=tmp / ".env.patient")
    proutes.DB_PATH = db_path
    rules = [r.rule for r in papp.url_map.iter_rules()]
    assert not any("guide" in r for r in rules), "8: the patient portal has no guides route"
    import site_app
    srules = [r.rule for r in site_app.create_site_app().url_map.iter_rules()]
    assert not any("guide" in r for r in srules), "8: the public site has no guides route"
    assert "/guides/ask" in den.get("/").text and "/guides/ask" in ast.get("/").text and "/guides" not in adm.get("/").text, \
        "8: the sidebar links the page for dentist and assistant only"
    conn.close()


def selftest():
    if not TOOLS:
        print("SKIPPED guides_selftest: sips, tesseract or sandbox-exec missing - nothing claimed")
        return
    separation()
    with tempfile.TemporaryDirectory() as tmp:
        review_and_roles(tmp)
    with tempfile.TemporaryDirectory() as tmp:
        conn, lib, ids, devices = evaluation(tmp)
        verification(conn, ids, devices)
        same_question(conn, ids, devices)
        two_part_and_conflicts(conn, ids, devices)
        booking_permission(conn, ids, devices)
        title_alone(conn, ids, devices)
        offline(conn, devices)
        lifecycle(conn, lib, ids, devices)
        conn.close()
    with tempfile.TemporaryDirectory() as tmp:
        backup_and_patients(tmp)
    with tempfile.TemporaryDirectory() as tmp:
        routes(tmp)
    print("guides_selftest: ok")


if __name__ == "__main__":
    selftest()
    sys.exit(0)
