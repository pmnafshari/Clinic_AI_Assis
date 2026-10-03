"""P27 booking and completion record for demo imaging requests: the locked acceptance cases K01-K20.

Synthetic patients, requests, appointments and files in a temp folder; the clinic clock is pinned to a Wednesday in
2031 so every booking is in the future whatever the machine's date. No model, no guides, no provider.
"""
import re
import sqlite3
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path

import appointments
import clinic_time
import documents as docs
import imaging_bookings as ib
import imaging_requests as ir
import patient_id

D, D2, A, A2, ADM = ("drossi", "dentist"), ("dbianchi", "dentist"), ("aassist", "assistant"), ("breception", "assistant"), ("anadmin", "admin")
TODAY = datetime(2031, 3, 5, 8, 0)                          # a Wednesday
CF = {"paola_a": "ZZKP000000000001", "paola_b": "ZZKP000000000002", "none": "ZZKN000000000003", "vera": "ZZKV000000000004"}
SLOT = "2031-03-06T10:00"                                   # Thursday, inside hours


def setup(tmp):
    from storage import init_db
    tmp = Path(tmp)
    clinic_time.now = lambda env=None: TODAY
    docs.DOC_ROOT = tmp / "documents"
    docs.DOC_CHROMA_PATH = str(tmp / "doc_chroma")
    docs.SLOT_DIR = tmp / "slots"
    docs.unindex = lambda ids: None
    docs._index = lambda r: None
    (tmp / "db").mkdir(exist_ok=True)
    conn = init_db(str(tmp / "db" / "clinic.sqlite"))
    import availability
    availability.seed_fixture_hours(conn)
    for dentist in ("drossi", "dbianchi"):
        for wd in range(5):
            conn.execute("INSERT OR IGNORE INTO dentist_schedule (dentist, weekday, starts, ends) VALUES (?, ?, '08:00',"
                         " '19:00')", (dentist, wd))
    pids = {k: patient_id.seed_patient(conn, cf, "Paola Bianchi" if k.startswith("paola") else k.title(), "+393401110001")
            for k, cf in CF.items()}
    conn.commit()
    return conn, pids


def active(conn, pid, exam="opg", token=None):
    rid, _w = ir.create_draft(conn, pid, exam, "", "", *D, token=token or f"t-{pid}-{exam}-{datetime.now().timestamp()}")
    ir.activate(conn, rid, pid, 1, *D)
    return rid


def refused(fn, code=None, perm=False):
    try:
        fn()
    except PermissionError:
        assert perm, "refused with PermissionError where a state error was expected"
        return "perm"
    except (ib.BookingError, ir.RequestError, LookupError, ValueError) as e:
        assert not perm, f"expected a permission refusal, got {e!r}"
        if code:
            assert getattr(e, "code", "lookup" if isinstance(e, LookupError) else "value") == code, \
                f"refused for {getattr(e, 'code', type(e).__name__)} ({e}), not {code}"
        return getattr(e, "code", "other")
    raise AssertionError("HARD FAIL - the action was not refused")


def appts(conn):
    return conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0]


def service(tmp):
    conn, pids = setup(tmp)
    pa, pb = pids["paola_a"], pids["paola_b"]
    opg = active(conn, pa, "opg", "k-opg")
    bw = active(conn, pa, "bitewing", "k-bw")

    # K01 book the active request: one booking, one ordinary appointment with no note
    bid = ib.book(conn, opg, pa, 1, "drossi", SLOT, 20, *A, token="b1")
    b = ib.get(conn, bid)
    ap = conn.execute("SELECT * FROM appointments WHERE id = ?", (b["appointment_id"],)).fetchone()
    assert b["state"] == "booked" and b["request_id"] == opg and ap["status"] == "booked" and ap["patient_id"] == pa \
        and ap["note"] is None, "K01: one booking, an ordinary appointment, nothing written in its note"
    # K03 double submit: the same one-time token is one booking - the first one back, not an error
    try:
        again = ib.book(conn, opg, pa, 1, "drossi", SLOT, 20, *A, token="b1")
    except Exception as e:
        raise AssertionError(f"K03: a resubmit must return the first booking, not fail: {e!r}")
    assert again == bid and appts(conn) == 1, "K03: HARD FAIL - a resubmitted booking made a second appointment"
    # a second booking for the same request (another token) is refused while one is live
    refused(lambda: ib.book(conn, opg, pa, 1, "drossi", "2031-03-06T11:00", 20, *A, token="b1x"), "already_booked")

    # K02 draft, cancelled, superseded or absent request: never booked
    draft, _w = ir.create_draft(conn, pa, "periapical", "", "", *D, token="k-draft")
    refused(lambda: ib.book(conn, draft, pa, 1, "drossi", "2031-03-06T12:00", 20, *A, token="b2"), "not_active")
    gone = active(conn, pa, "cbct", "k-cbct")
    ir.cancel(conn, gone, pa, 1, "not needed", *D)
    refused(lambda: ib.book(conn, gone, pa, 1, "drossi", "2031-03-06T12:00", 20, *A, token="b3"), "not_active")
    refused(lambda: ib.book(conn, 99999, pa, 1, "drossi", "2031-03-06T12:00", 20, *A, token="b4"), "lookup")
    refused(lambda: ib.book(conn, bw, pa, 7, "drossi", "2031-03-06T12:00", 20, *A, token="b5"), "stale")
    # K05 the slot was taken between opening the page and booking: refused, nothing written
    appointments.book(conn, pids["none"], "drossi", "2031-03-06T13:00", 30)
    before = (appts(conn), conn.execute("SELECT COUNT(*) FROM imaging_bookings").fetchone()[0])
    refused(lambda: ib.book(conn, bw, pa, 1, "drossi", "2031-03-06T13:00", 20, *A, token="b6"), "value")
    assert (appts(conn), conn.execute("SELECT COUNT(*) FROM imaging_bookings").fetchone()[0]) == before, \
        "K05: HARD FAIL - a refused booking left an appointment or a booking row"
    # K05b the booking fails after its slot was taken: the appointment goes with it (one transaction)
    real_event = ir._event

    def boom(*a, **k):
        raise RuntimeError("late failure")
    ir._event = boom
    try:
        ib.book(conn, bw, pa, 1, "drossi", "2031-03-06T14:30", 20, *A, token="b6b")
        raise AssertionError("K05b: the forced failure did not happen")
    except RuntimeError:
        pass
    finally:
        ir._event = real_event
    assert (appts(conn), conn.execute("SELECT COUNT(*) FROM imaging_bookings").fetchone()[0]) == before, \
        "K05b: HARD FAIL - a booking that failed after taking the slot left the appointment behind"
    # K06 outside hours, a weekend, an off-roster dentist, the DST gap: refused like any booking
    for when, who in (("2031-03-06T03:00", "drossi"), ("2031-03-08T10:00", "drossi"), ("2031-03-06T10:30", "nobody"),
                      ("2031-03-30T02:30", "drossi")):
        refused(lambda: ib.book(conn, bw, pa, 1, who, when, 20, *A, token=f"b-{when}-{who}"), "value")
    # K16 admin cannot book; nobody but a dentist completes
    refused(lambda: ib.book(conn, bw, pa, 1, "drossi", "2031-03-06T14:00", 20, *ADM, token="b7"), perm=True)
    # K07 another patient's request, even a same-name one
    refused(lambda: ib.book(conn, bw, pb, 1, "drossi", "2031-03-06T14:00", 20, *A, token="b8"), "lookup")
    refused(lambda: ib.resolve(conn, bid, pb, "closed", "x", *A), "lookup")

    # K04 two receptionists book the same request at the same moment: one booking
    db = conn.execute("PRAGMA database_list").fetchone()[2]
    outcomes, barrier = [], threading.Barrier(2)

    def go(who, tok, when):
        from storage import connect
        c = connect(db)
        barrier.wait()
        try:
            ib.book(c, bw, pa, 1, "drossi", when, 20, *who, token=tok)
            outcomes.append("ok")
        except ib.BookingError as e:
            outcomes.append(e.code)
        finally:
            c.close()
    threads = [threading.Thread(target=go, args=a) for a in ((A, "c1", "2031-03-06T15:00"), (A2, "c2", "2031-03-06T16:00"))]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(outcomes) == ["already_booked", "ok"], f"K04: HARD FAIL - {outcomes}"
    live = conn.execute("SELECT COUNT(*) FROM imaging_bookings WHERE series_id = ? AND state != 'resolved'",
                        (ir.get(conn, bw)["series_id"],)).fetchone()[0]
    assert live == 1 and appts(conn) == 3, "K04: one live booking and one appointment for the request"
    bw_booking = conn.execute("SELECT id FROM imaging_bookings WHERE request_id = ? AND state = 'booked'", (bw,)).fetchone()[0]

    # K08 the patient cancels on the portal (appointments.cancel): the booking needs reception action, nothing rebooked
    appointments.cancel(conn, ib.get(conn, bid)["appointment_id"])
    b = ib.get(conn, bid)
    assert b["state"] == "needs_action" and b["flag"] == "appointment_cancelled", "K08: HARD FAIL - a cancellation went unnoticed"
    assert [t["id"] for t in ib.tasks(conn, *A)] == [bid], "K08: the task is listed for reception"
    assert appts(conn) == 3, "K08: nothing is rebooked automatically"
    refused(lambda: ib.resolve(conn, bid, pa, "closed", "", *A), "note_needed")
    refused(lambda: ib.resolve(conn, bid, pa, "reattached", "x", *A), "resolution")
    # a resolution that exists, but not for this flag, is refused before it can do anything
    refused(lambda: ib.resolve(conn, bid, pa, "kept_unlinked", "x", *A), "resolution")
    assert ib.get(conn, bid)["state"] == "needs_action", "K08: a refused resolution changes nothing"
    ib.resolve(conn, bid, pa, "closed", "patient will call back to rebook", *A)
    b = ib.get(conn, bid)
    assert b["state"] == "resolved" and b["resolved_by"] == "aassist" and b["resolution_note"], "K08: resolved with who and why"
    assert ib.tasks(conn, *A) == [], "K08: resolved tasks leave the list, and only then"
    bid2 = ib.book(conn, opg, pa, 1, "drossi", "2031-03-07T10:00", 20, *A, token="b9")       # rebooked explicitly

    # K09 a move made outside the booking (staff Appointments page) is flagged; one made through it is not
    appointments.reschedule(conn, ib.get(conn, bid2)["appointment_id"], "2031-03-07T11:00", 20)
    assert ib.get(conn, bid2)["state"] == "needs_action" and ib.get(conn, bid2)["flag"] == "appointment_moved", \
        "K09: a move elsewhere needs reception to confirm it"
    ib.resolve(conn, bid2, pa, "accepted_new_time", "", *A)
    assert ib.get(conn, bid2)["state"] == "booked", "K09: accepting the new time keeps the link"
    ib.move(conn, bid2, pa, "2031-03-07T12:00", 20, *A)
    assert ib.get(conn, bid2)["state"] == "booked" and ib.get(conn, bid2)["appt_starts_at"].startswith("2031-03-07T11"), \
        "K09: a move through the booking follows it (stored in UTC; 12:00 in Rome is 11:00 UTC)"
    assert "moved" in [e["action"] for e in ir.history(conn, opg, pa, *D)], "K09: the move is in the history"

    # K10 the dentist cancels the request: appointment untouched, booking flagged, never shown as valid
    ap_before = dict(conn.execute("SELECT * FROM appointments WHERE id = ?", (ib.get(conn, bw_booking)["appointment_id"],)).fetchone())
    ir.cancel(conn, bw, pa, 1, "plan changed", *D)
    ap_after = dict(conn.execute("SELECT * FROM appointments WHERE id = ?", (ap_before["id"],)).fetchone())
    assert ap_after == ap_before, "K10: HARD FAIL - a request cancellation changed the patient's appointment"
    b = ib.get(conn, bw_booking)
    assert b["state"] == "needs_action" and b["flag"] == "request_cancelled", "K10: flagged at once"
    line = ib.status_line(conn, ir.get(conn, bw))
    assert "booked" not in line.lower() or "needs" in line.lower(), f"K10: shown as a valid booking: {line}"
    refused(lambda: ib.resolve(conn, bw_booking, pa, "reattached", "x", *A), "resolution")
    # keeping the time would show a cancelled request's booking as valid again
    refused(lambda: ib.resolve(conn, bw_booking, pa, "accepted_new_time", "", *A), "resolution")
    assert ib.get(conn, bw_booking)["state"] == "needs_action", "K10: HARD FAIL - a cancelled request's booking came back"
    ib.resolve(conn, bw_booking, pa, "appointment_cancelled", "told the patient", *A)
    assert conn.execute("SELECT status FROM appointments WHERE id = ?", (ap_before["id"],)).fetchone()[0] == "cancelled", \
        "K10: reception's resolution cancels it, and only then"
    assert ib.get(conn, bw_booking)["state"] == "resolved", "K10: our own cancellation is the resolution, not a new task"

    # K11 the dentist revises the request: flagged when the new version is activated, never moved silently
    rev, _w = ir.revise(conn, opg, pa, "opg", "", "right side", *D, token="k-rev")
    assert ib.get(conn, bid2)["state"] == "booked", "K11: while v1 is active its booking stands"
    ir.activate(conn, rev, pa, 2, *D)
    b = ib.get(conn, bid2)
    assert b["state"] == "needs_action" and b["flag"] == "request_revised" and b["request_id"] == opg, \
        "K11: HARD FAIL - the booking moved to the new version by itself"
    assert ib.live_for_request(conn, rev) is None, "K11: the new version has no booking until reception says so"
    ib.resolve(conn, bid2, pa, "reattached", "same exam, patient keeps the slot", *A)
    moved = ib.live_for_request(conn, rev)
    assert moved and moved["appointment_id"] == b["appointment_id"] and moved["request_version"] == 2, \
        "K11: re-attached explicitly to version 2"

    # K12-K15 completion: dentist only, a linked, confirmed, verified image of this patient
    from patient_files_selftest import png
    mine = docs.ingest(conn, pa, png(21), "opg-2031.png", *D)
    docs.confirm(conn, mine, pa, *D)
    not_linked = docs.ingest(conn, pa, png(22), "other.png", *D)
    docs.confirm(conn, not_linked, pa, *D)
    unconfirmed = docs.ingest(conn, pa, b"a scan waiting for review", "scan.txt", *D)
    theirs = docs.ingest(conn, pb, png(23), "opg-b.png", *D)
    docs.confirm(conn, theirs, pb, *D)
    ir.link_file(conn, rev, pa, mine, *D, verified=True)
    for who in (A, ADM):
        refused(lambda: ib.complete(conn, rev, pa, 2, mine, True, *who), perm=True)       # K16
    refused(lambda: ib.complete(conn, rev, pa, 2, mine, False, *D), "not_verified")
    refused(lambda: ib.complete(conn, rev, pa, 2, not_linked, True, *D), "not_linked")
    refused(lambda: ib.complete(conn, rev, pa, 2, unconfirmed, True, *D), "not_linked")
    refused(lambda: ib.complete(conn, rev, pa, 2, theirs, True, *D), "not_linked")
    refused(lambda: ib.complete(conn, opg, pa, 1, mine, True, *D), "not_active")        # superseded version
    refused(lambda: ib.complete(conn, rev, pa, 1, mine, True, *D), "stale")
    ib.complete(conn, rev, pa, 2, mine, True, *D)
    refused(lambda: ib.complete(conn, rev, pa, 2, mine, True, *D), "already_done")        # K14
    done = ib.completion_for(conn, rev)
    assert done and done["document_id"] == mine and done["recorded_by"] == "drossi", "K12: recorded"
    line = ib.status_line(conn, ir.get(conn, rev))
    assert "Done" in line and "opg-2031" not in line, f"K12: reception sees done and no file: {line}"
    import patient_files as pf
    assert not pf.published(conn, mine, pa) and docs.row(conn, mine)["status"] == "confirmed", \
        "K12: HARD FAIL - completion published or changed the file"
    # a completed request cannot be revised or cancelled until the completion is reversed
    refused(lambda: ir.cancel(conn, rev, pa, 2, "x", *D), "completed")
    refused(lambda: ib.reverse(conn, rev, pa, "", *D), "note_needed")
    ib.reverse(conn, rev, pa, "wrong image chosen", *D)
    assert ib.completion_for(conn, rev) is None and "Done" not in ib.status_line(conn, ir.get(conn, rev)), "K15: reversed"
    refused(lambda: ib.reverse(conn, rev, pa, "again", *D), "not_done")
    events = [e["action"] for e in ir.history(conn, rev, pa, *D)]
    assert "completed" in events and "completion_reversed" in events, "K15: both are in the history"
    audit = " ".join(f"{t} {r}" for t, r in conn.execute("SELECT target, reason FROM audit_log WHERE action LIKE 'imaging_%'"))
    for secret in ("right side", "opg-2031", CF["paola_a"], "wrong image"):
        assert secret not in audit, f"the audit trail carries {secret!r}"
    return conn, pids, rev, moved["id"]


def patient_visible(tmp, conn, pids, rev, booking):
    # K17 the portal shows an ordinary appointment; K18 the reminder says nothing about imaging
    from patient_app import create_patient_app, routes as proutes
    import patient_auth
    import consent
    import reminders
    pa = pids["paola_a"]
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    proutes.DB_PATH = db_path
    papp = create_patient_app(env_path=Path(tmp) / ".env.patient")
    papp.config["TESTING"] = True
    patient_auth.issue_pin(CF["paola_a"], conn, "drossi")
    conn.execute("UPDATE patient_credentials SET must_change_pin = 0")
    conn.commit()
    token = patient_auth.create_patient_session(conn, CF["paola_a"])
    client = papp.test_client()
    client.set_cookie(patient_auth.PATIENT_COOKIE_NAME, token)
    client.set_cookie("patient_lang", "en")
    page = client.get("/appointments").text
    assert "2031" in page or "March" in page or "mar" in page.lower(), "K17: the appointment is on the portal"
    for secret in ("OPG", "panoramic", "imaging", "Imaging", "demo", "right side", f"request {rev}", "Bitewing"):
        assert secret not in page, f"K17: HARD FAIL - the portal shows {secret!r}"
    consent.record(conn, pa, "messaging", True, "drossi", "dentist")
    reminders.plan(conn, now=clinic_time.to_utc(datetime(2031, 3, 5, 12, 0)))
    jobs = conn.execute("SELECT * FROM reminder_jobs WHERE patient_id = ?", (pa,)).fetchall()
    assert jobs, "K18: a reminder is planned for the booked appointment"
    for job in jobs:
        text = reminders.body(conn, job)
        for secret in ("OPG", "panoramic", "imaging", "radiograph", "X-ray", "demo"):
            assert secret.lower() not in text.lower(), f"K18: HARD FAIL - the reminder says {secret!r}: {text}"


def lifecycle(tmp, conn, pids, rev):
    # K19 backup/restore, merge, erasure; K20 nothing reaches the guides store or a model
    import backup
    import erasure
    import patient_identity
    root = Path(tmp)
    key = root / "k"
    backup.init_key(key)
    conn.commit()
    archive = backup.create(data_root=root, dest=root / "backups", key_path=key)["archive"]
    backup.restore(archive, root / "restored", key, apply=True)
    rc = sqlite3.connect(root / "restored" / "db" / "clinic.sqlite")
    for table in ("imaging_bookings", "imaging_completions"):
        restored, live = (rc.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                          conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        assert live > 0 and restored == live, f"K19: {table} not restored ({restored} of {live})"
    rc.close()
    pa, pb, vera = pids["paola_a"], pids["paola_b"], pids["vera"]
    vb = active(conn, pb, "opg", "k-merge")
    ib.book(conn, vb, pb, 1, "dbianchi", "2031-03-06T10:00", 20, *A, token="m1")
    ok, msg = patient_identity.merge(conn, CF["paola_b"], CF["paola_a"], "anadmin", "admin", sorted_root=root / "sorted")
    assert ok, msg
    assert conn.execute("SELECT patient_id FROM imaging_bookings WHERE request_id = ?", (vb,)).fetchone()[0] == pa, \
        "K19: a merge brings the folded record's bookings"
    vr = active(conn, vera, "cbct", "k-erase")
    ib.book(conn, vr, vera, 1, "drossi", "2031-03-06T17:00", 20, *A, token="e1")
    erasure.erase(conn, vera, "drossi", "dentist", req_id=None, sorted_root=root / "sorted", drop_dir=root / "drop",
                  undo_log=root / "u.jsonl", exports_dir=root / "exports", tombstones=root / "t", write_tombstone=False)
    for table in ("imaging_bookings", "imaging_completions"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE patient_id = ?", (vera,)).fetchone()[0] == 0, \
            f"K19: erasure left rows in {table}"
    src = Path(ib.__file__).read_text()
    for banned in ("clinic_guides", "model_answer", "local_urlopen", "guides.sqlite"):
        assert banned not in src, f"K20: imaging_bookings mentions {banned}"
    assert not (root / "db" / "guides.sqlite").exists(), "K20: HARD FAIL - booking or completion touched the guides store"


def routes(tmp):
    # K07 K16 through the staff pages; the task list; CSRF
    import app.db as app_db
    import web_session
    from app import create_app
    from werkzeug.security import generate_password_hash
    tmp = Path(tmp)
    conn, pids = setup(tmp)
    import clinic_guides
    clinic_guides.DB_PATH = str(tmp / "db" / "guides.sqlite")
    db_path = str(tmp / "db" / "clinic.sqlite")
    app_db.DB_PATH = db_path
    app_db.CHROMA_PATH = str(tmp / "chroma")
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
    den, ast, adm = client(*D), client(*A), client(*ADM)

    def csrf(html):
        return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
    pa = pids["paola_a"]
    cfa, cfb = CF["paola_a"], CF["paola_b"]
    rid = active(conn, pa, "opg", "r-opg")
    page = ast.get(f"/patients/{cfa}/imaging/{rid}").text
    assert "Book an appointment for this request" in page, "reception books from the handoff"
    tok = re.search(r'name="booking_token" value="([^"]+)"', page).group(1)
    form = {"csrf_token": csrf(page), "version": "1", "dentist": "drossi", "day": "2031-03-06", "time": "10:00",
            "minutes": "20", "booking_token": tok}
    ast.post(f"/patients/{cfa}/imaging/{rid}/book", data=form)
    ast.post(f"/patients/{cfa}/imaging/{rid}/book", data=form)
    assert conn.execute("SELECT COUNT(*) FROM imaging_bookings").fetchone()[0] == 1, "K03: one booking through the page"
    bid = conn.execute("SELECT id FROM imaging_bookings").fetchone()[0]
    page = ast.get(f"/patients/{cfa}/imaging/{rid}").text
    assert "Booked" in page and "2031" in page, "the handoff shows the booking"
    assert "Record completion" not in page, "K16: reception is never offered completion"
    # a dentist cancels the request: the task appears for reception
    dpage = den.get(f"/patients/{cfa}/imaging/{rid}").text
    den.post(f"/patients/{cfa}/imaging/{rid}/cancel", data={"csrf_token": csrf(dpage), "version": "1", "reason": "x"})
    tasks = ast.get("/imaging/tasks")
    assert tasks.status_code == 200 and "Paola Bianchi" in tasks.text and "needs reception action" in tasks.text.lower(), \
        "K10: the task list shows it"
    assert "/imaging/tasks" in ast.get("/").text and "/imaging/tasks" not in adm.get("/").text, "the sidebar links the list"
    assert adm.get("/imaging/tasks").status_code in (302, 403), "K16: admin has no task list"
    # K07 another patient's address, and a missing booking, answer the same
    other = ast.post(f"/patients/{cfb}/imaging/{rid}/bookings/{bid}/resolve",
                     data={"csrf_token": csrf(page), "resolution": "kept_unlinked", "note": "x"})
    missing = ast.post(f"/patients/{cfa}/imaging/{rid}/bookings/99999/resolve",
                       data={"csrf_token": csrf(page), "resolution": "kept_unlinked", "note": "x"})
    assert other.status_code == 404 and other.data == missing.data, "K07: HARD FAIL - another patient's booking answered"
    assert ib.get(conn, bid)["state"] == "needs_action", "K07: nothing changed"
    r = ast.post(f"/patients/{cfa}/imaging/{rid}/bookings/{bid}/resolve", data={"resolution": "kept_unlinked", "note": "x"})
    assert r.status_code == 400 and ib.get(conn, bid)["state"] == "needs_action", "a post without CSRF changes nothing"
    page = ast.get(f"/patients/{cfa}/imaging/{rid}").text
    ast.post(f"/patients/{cfa}/imaging/{rid}/bookings/{bid}/resolve",
             data={"csrf_token": csrf(page), "resolution": "kept_unlinked", "note": "kept for a check-up"})
    assert ib.get(conn, bid)["state"] == "resolved" and ib.get(conn, bid)["resolution"] == "kept_unlinked", "resolved"
    assert "Paola Bianchi" not in ast.get("/imaging/tasks").text, "the resolved task leaves the list"
    conn.close()


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        conn, pids, rev, booking = service(tmp)
        patient_visible(tmp, conn, pids, rev, booking)
        lifecycle(tmp, conn, pids, rev)
        conn.close()
    with tempfile.TemporaryDirectory() as tmp:
        routes(tmp)
    print("imaging_bookings_selftest: ok")


if __name__ == "__main__":
    selftest()
    sys.exit(0)
