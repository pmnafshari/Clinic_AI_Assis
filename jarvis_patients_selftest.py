"""J03 slice 2 (owner-authorised 2026-10-12): read and edit by voice, and one sign-in, through the clinic's own checks.
A spoken name only proposes patients; what is read stays on the device's screen; a change is saved only when the doctor
picks the patient there, through the clinic's own pending action. The Clinic tab is signed in from Jarvis's sign-in by a
one-time link, and that session ends with it.

    .venv/bin/python jarvis_patients_selftest.py

1. read: without a doctor signed in - refused, audited, no patient named; signed in - the matching patients (sound-alike
   names, same names all offered, none or too many refused); the next appointment of each, audited per patient shown;
   reception refused; by codice fiscale for the patient just handled
2. edit: only the phone, only an Italian number, shown before/after per match, nothing written until the save; the
   save goes through the clinic's own pending action (undo log, audit); a draft is used once, by its own device and
   doctor, in time, for a proposed patient, and refused if the record changed since it was shown
3. one sign-in: the link needs a doctor signed in on the device; used once, in 60 s; the clinic session it starts ends
   with the Jarvis sign-in (sign out, restart, 00:00, another doctor) - a typed clinic session is never touched
4. over HTTP: device credential only; the routes answer as the functions do; the hand-off link signs a browser in and
   the clinic sends it to the login page once the Jarvis sign-in has ended
"""
import json
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import clinic_time
import jarvis_link as jl
import web_session
from jarvis_notes_selftest import PASSWORD, delegate, refused, setup
from patient_files_selftest import staff_client

GIULIA, PAOLA_A, PAOLA_B, MARCO = "SNTGLI51R49D612Y", "RSSP850010150900", "RSPS850010150900", "GLLMRC67A11F839U"


def book(conn, cf):
    """A booked appointment for `cf` tomorrow at 10:30 clinic time (written as the booking writes it; the test clinic
    has no opening hours) and a cancelled one before it, which must not count."""
    import patient_id
    from datetime import datetime
    day = date.today() + timedelta(days=1)
    for status, hhmm in (("cancelled", "09:00"), ("booked", "10:30")):
        start = clinic_time.to_utc(datetime.fromisoformat(f"{day.isoformat()}T{hhmm}"))
        conn.execute("INSERT INTO appointments (patient_id, dentist, starts_at, minutes, status, created_at, updated_at)"
                     " VALUES (?, 'drossi', ?, 30, ?, ?, ?)", (patient_id.resolve(conn, cf), clinic_time.to_storage(start),
                                                                status, clinic_time.stamp(), clinic_time.stamp()))
    conn.commit()
    return day.isoformat()


def audit(conn, action):
    return conn.execute("SELECT * FROM audit_log WHERE action = ? ORDER BY id DESC", (action,)).fetchall()


def phone(conn, cf):
    return conn.execute("SELECT phone FROM patients WHERE codice_fiscale = ?", (cf,)).fetchone()[0]


def read(conn, device, other):
    # 1.
    jl.logout(conn, device, "test")
    n = len(audit(conn, "jarvis_read"))
    refused(lambda: jl.read_patients(conn, device, "Giulia Santoro", "open"), "auth_required")
    rows = audit(conn, "jarvis_read")
    assert len(rows) == n + 1 and rows[0]["allowed"] == 0 and GIULIA not in (rows[0]["target"] or ""), \
        "1: HARD FAIL - a refused read names a patient, or is not audited"
    jl.login(conn, device, "drossi", PASSWORD)
    out = jl.read_patients(conn, device, "Julia Santoro", "open")
    assert out == {"choices": [{"cf": GIULIA, "name": "Giulia Santoro"}]}, f"1: a sound-alike name proposes ({out})"
    assert audit(conn, "jarvis_read")[0]["target"] == f"jarvis_device:{device['id']}", "1: proposals name the device"
    out = jl.read_patients(conn, device, "Paola Rossi", "open")
    assert sorted(c["cf"] for c in out["choices"]) == sorted([PAOLA_A, PAOLA_B]), "1: HARD FAIL - same names not all offered"
    refused(lambda: jl.read_patients(conn, device, "Nobody Here", "open"), "no_match")
    refused(lambda: jl.read_patients(conn, device, "Giulia Santoro", "anything"), "empty")
    old, jl.MAX_MATCHES = jl.MAX_MATCHES, 1
    try:
        refused(lambda: jl.read_patients(conn, device, "Paola", "next"), "too_many")
    finally:
        jl.MAX_MATCHES = old
    when = book(conn, GIULIA)
    out = jl.read_patients(conn, device, "Giulia Santoro", "next")
    nxt = out["choices"][0]["next"]
    assert nxt["date"] == when and nxt["time"] == "10:30" and nxt["dentist"] == "drossi" and nxt["minutes"] == 30 \
        and nxt["weekday"] == date.fromisoformat(when).strftime("%A"), f"1: the next booked appointment ({nxt})"
    import patient_id
    row = audit(conn, "jarvis_read")[0]
    assert row["reason"] == "next_appointment" and row["target"] == patient_id.resolve(conn, GIULIA), \
        "1: HARD FAIL - an appointment shown without an audit row against that patient"
    out = jl.read_patients(conn, device, "Paola Rossi", "next")
    assert [c["next"] for c in out["choices"]] == [None, None], "1: no appointment booked -> none"
    out = jl.read_patients(conn, device, "", "next", cf=MARCO)
    assert [c["cf"] for c in out["choices"]] == [MARCO], "1: the patient just handled, by codice fiscale"
    refused(lambda: jl.read_patients(conn, device, "", "next", cf="ZZZZZZ00Z00Z000Z"), "no_match")
    refused(lambda: jl.read_patients(conn, jl.authenticate(conn, other), "Giulia Santoro", "open"), "auth_required")
    return when


def edit(app, db_path, conn, device, other, token):
    # 2.
    applied = []

    def apply(pending, role, username):
        import agent
        applied.append((pending, role, username))
        agent.apply_pending_action(pending, conn, role, username, log_path=str(Path(db_path).parent / "undo.jsonl"))
    refused(lambda: jl.draft_edit(conn, device, "Marco Gallo", "patient_name", "Mario Gallo"), "field")
    refused(lambda: jl.draft_edit(conn, device, "Marco Gallo", "phone", "555 1234"), "value")
    before = phone(conn, MARCO)
    d = jl.draft_edit(conn, device, "Marco Galo", "phone", "333 123 4567")
    assert d["value"] == "+393331234567" and d["choices"] == [{"cf": MARCO, "name": "Marco Gallo", "before": before}], \
        f"2: one proposal, the number as the clinic stores it, the current one beside it ({d})"
    assert phone(conn, MARCO) == before and not applied, "2: HARD FAIL - a draft wrote"
    refused(lambda: jl.commit_edit(conn, device, d["draft"], GIULIA, apply), "patient")
    refused(lambda: jl.commit_edit(conn, device, d["draft"], MARCO, apply), "gone")
    assert phone(conn, MARCO) == before and not applied, "2: HARD FAIL - an unproposed patient written, or a draft reused"
    d = jl.draft_edit(conn, device, "Marco Gallo", "phone", "+39 333 123 4567")
    refused(lambda: jl.commit_edit(conn, jl.authenticate(conn, other), d["draft"], MARCO, apply), "gone")
    d = jl.draft_edit(conn, device, "Marco Gallo", "phone", "3331234567")
    late = clinic_time.now_utc() + timedelta(seconds=jl.NOTE_SECONDS + 1)
    refused(lambda: jl.commit_edit(conn, device, d["draft"], MARCO, apply, now=late), "gone")
    d = jl.draft_edit(conn, device, "Marco Gallo", "phone", "3331234567")
    note = jl.draft_note(conn, device, "Marco Gallo", "a note")
    try:                                                                  # a note's draft is no change
        jl.commit_edit(conn, device, note["draft"], MARCO, apply)
    except jl.LinkError as e:
        assert e.code == "gone", f"2: a note's draft refused for the wrong reason ({e.code})"
    except Exception as e:
        raise AssertionError(f"2: a note's draft broke the save instead of being refused ({e!r})") from None
    else:
        raise AssertionError("2: HARD FAIL - a note's draft saved as a change")
    conn.execute("UPDATE patients SET phone = '+390612345678' WHERE codice_fiscale = ?", (MARCO,))
    conn.commit()
    refused(lambda: jl.commit_edit(conn, device, d["draft"], MARCO, apply), "changed")
    assert phone(conn, MARCO) == "+390612345678" and not applied, "2: HARD FAIL - saved over a number changed since"
    d = jl.draft_edit(conn, device, "Marco Gallo", "phone", "3331234567")
    out = jl.commit_edit(conn, device, d["draft"], MARCO, apply)
    assert out == {"saved": True, "cf": MARCO, "name": "Marco Gallo", "field": "phone", "value": "+393331234567"}, out
    assert phone(conn, MARCO) == "+393331234567", "2: saved"
    pending, role, username = applied[-1]
    assert pending["tool"] == "update_field" and pending["cf"] == MARCO and pending["before"] == "+390612345678" \
        and (role, username) == ("dentist", "drossi"), "2: the clinic's own pending action, as the signed-in doctor"
    undo = [json.loads(x) for x in (Path(db_path).parent / "undo.jsonl").read_text().splitlines()]
    assert undo[-1]["codice_fiscale"] == MARCO and undo[-1]["before"] == "+390612345678", "2: undo log written"
    assert audit(conn, "update_field")[0]["allowed"] == 1 and audit(conn, "jarvis_edit")[0]["reason"] == "jarvis_voice"
    # the doctor signed out between draft and save; then reception: nothing written
    d = jl.draft_edit(conn, device, "Marco Gallo", "phone", "3339999999")
    jl.logout(conn, device, "test")
    refused(lambda: jl.commit_edit(conn, device, d["draft"], MARCO, apply), "auth_required")
    delegate(app, db_path, conn, token, ("aassist", "assistant"))
    refused(lambda: jl.draft_edit(conn, device, "Marco Gallo", "phone", "3339999999"), "role")
    refused(lambda: jl.read_patients(conn, device, "Marco Gallo", "open"), "role")
    assert phone(conn, MARCO) == "+393331234567" and len(applied) == 1, "2: HARD FAIL - written without a doctor"
    jl.logout(conn, device, "test")


def handoff(app, db_path, conn, device, other, token):
    # 3.
    refused(lambda: jl.handoff_ticket(conn, device), "auth_required")
    delegate(app, db_path, conn, token, ("drossi", "dentist"))            # a browser session handed over (J00)
    refused(lambda: jl.handoff_ticket(conn, device), "auth_required")      # not a sign-in on the device
    typed = staff_client(app, db_path, "drossi", "dentist")               # the doctor's own typed clinic session
    jl.login(conn, device, "drossi", PASSWORD)
    t = jl.handoff_ticket(conn, device)
    session, username, role = jl.use_handoff(conn, t)
    assert (username, role) == ("drossi", "dentist") and web_session.load_session(conn, session), "3: signed in"
    refused(lambda: jl.use_handoff(conn, t), "gone")
    refused(lambda: jl.use_handoff(conn, "made-up-ticket"), "gone")
    h = web_session.token_hash(session)
    assert not jl.handoff_over(conn, h), "3: live while the Jarvis sign-in is"
    until = clinic_time.read_instant(jl.current_delegation(conn, device["id"])["expires_at"])
    assert jl.handoff_over(conn, h, until), "3: HARD FAIL - the hand-off session outlives 00:00"
    t = jl.handoff_ticket(conn, device)
    late = clinic_time.now_utc() + timedelta(seconds=jl.HANDOFF_SECONDS + 1)
    refused(lambda: jl.use_handoff(conn, t, now=late), "gone")
    t = jl.handoff_ticket(conn, device)
    jl.logout(conn, device, "signed_out")
    refused(lambda: jl.use_handoff(conn, t), "gone")                      # a link from an ended sign-in
    assert jl.handoff_over(conn, h) and web_session.load_session(conn, session) is None, \
        "3: HARD FAIL - the Clinic tab stayed signed in after Jarvis signed out"
    assert typed.get("/").status_code == 200, "3: HARD FAIL - a typed clinic session ended with Jarvis's"
    jl.login(conn, device, "drossi", PASSWORD)
    session, _, _ = jl.use_handoff(conn, jl.handoff_ticket(conn, device))
    t = jl.handoff_ticket(conn, device)
    jl.login(conn, device, "dbianchi", PASSWORD)                         # another doctor signs Jarvis in
    assert web_session.load_session(conn, session) is None, "3: HARD FAIL - the first doctor's tab stayed signed in"
    refused(lambda: jl.use_handoff(conn, t), "gone")
    session, username, _ = jl.use_handoff(conn, jl.handoff_ticket(conn, device))
    assert username == "dbianchi", "3: the link signs in whoever signed Jarvis in"
    jl.logout(conn, device, "restarted")                                 # Jarvis starting again signs out
    assert web_session.load_session(conn, session) is None, "3: HARD FAIL - kept after Jarvis restarted"
    refused(lambda: jl.handoff_ticket(conn, jl.authenticate(conn, other)), "auth_required")
    rows = audit(conn, "jarvis_handoff")
    assert any(r["allowed"] == 1 for r in rows) and any(r["allowed"] == 0 for r in rows), "3: audited, refusals too"


def http(app, conn, device, token, when):
    # 4.
    anon = app.test_client()
    h = {"Authorization": f"Bearer {token}"}
    for path, body in (("/api/jarvis/patients/read", {"patient": "Giulia Santoro", "want": "open"}),
                       ("/api/jarvis/patients/edit/draft", {"patient": "Marco Gallo", "field": "phone", "value": "3331"}),
                       ("/api/jarvis/patients/edit/commit", {"draft": "x", "cf": MARCO}), ("/api/jarvis/handoff", {})):
        assert anon.post(path, json=body).status_code == 401, f"4: {path} without the device credential"
    r = anon.post("/api/jarvis/patients/read", json={"patient": "Giulia Santoro", "want": "next"}, headers=h)
    assert r.status_code == 403 and r.get_json()["error"] == "auth_required" and "choices" not in r.get_json()
    assert anon.post("/api/jarvis/handoff", headers=h).status_code == 403, "4: no link without a sign-in"
    assert anon.post("/api/jarvis/login", json={"username": "drossi", "password": PASSWORD}, headers=h).status_code == 200
    r = anon.post("/api/jarvis/patients/read", json={"patient": "Giulia Santoro", "want": "next"}, headers=h)
    assert r.status_code == 200 and r.get_json()["choices"][0]["next"]["date"] == when, r.get_json()
    assert anon.post("/api/jarvis/patients/read", json={"patient": "x"}, headers=h).status_code == 400
    r = anon.post("/api/jarvis/patients/read", json={"patient": "Nobody", "want": "open"}, headers=h)
    assert r.status_code == 404 and r.get_json()["error"] == "no_match"
    r = anon.post("/api/jarvis/patients/edit/draft", json={"patient": "Marco Gallo", "field": "phone",
                                                           "value": "not a number"}, headers=h)
    assert r.status_code == 400 and r.get_json()["error"] == "value"
    import app.agent_routes as ar
    ar.UNDO_LOG = str(Path(app_db_dir(app)) / "undo-http.jsonl")
    r = anon.post("/api/jarvis/patients/edit/draft", json={"patient": "Marco Gallo", "field": "phone",
                                                           "value": "333 777 8888"}, headers=h)
    d = r.get_json()
    r = anon.post("/api/jarvis/patients/edit/commit", json={"draft": d["draft"], "cf": MARCO}, headers=h)
    assert r.status_code == 200 and phone(conn, MARCO) == "+393337778888", f"4: saved over HTTP ({r.get_json()})"
    assert anon.post("/api/jarvis/patients/edit/commit", json={"draft": d["draft"], "cf": MARCO},
                     headers=h).status_code == 410, "4: HARD FAIL - the same change saved twice"
    r = anon.post("/api/jarvis/handoff", headers=h)
    link = r.get_json()["path"]
    assert r.status_code == 200 and link.startswith("/jarvis/handoff/") and r.get_json()["seconds"] == 60
    tab = app.test_client()
    other_token = web_session.create_session(conn, "aassist", "assistant")     # this browser was reception's
    tab.set_cookie(web_session.COOKIE_NAME, other_token)
    r = tab.get(link)
    assert r.status_code == 302 and r.headers["Location"].endswith("/") and \
        r.headers.get("Referrer-Policy") == "no-referrer", f"4: the link signs the tab in ({r.status_code})"
    assert tab.get("/").status_code == 200, "4: the Clinic tab is signed in"
    assert tab.get("/api/session").get_json()["username"] == "drossi"
    assert web_session.load_session(conn, other_token) is None, "4: HARD FAIL - the browser's previous session lived on"
    # 00:00: the Jarvis sign-in has expired (nobody signed out): the clinic itself sends the tab to the login page
    row = conn.execute("SELECT id, expires_at FROM jarvis_delegations WHERE kind = 'login' AND revoked_at IS NULL"
                       " ORDER BY id DESC LIMIT 1").fetchone()
    conn.execute("UPDATE jarvis_delegations SET expires_at = ? WHERE id = ?",
                 (clinic_time.to_storage(clinic_time.now_utc() - timedelta(seconds=1)), row["id"]))
    conn.commit()
    r = tab.get("/")
    assert r.status_code == 302 and "/login" in r.headers["Location"], "4: HARD FAIL - the tab outlived 00:00"
    conn.execute("UPDATE jarvis_delegations SET expires_at = ? WHERE id = ?", (row["expires_at"], row["id"]))
    conn.commit()
    tab = app.test_client()
    r = tab.get(anon.post("/api/jarvis/handoff", headers=h).get_json()["path"])
    assert r.status_code == 302 and tab.get("/").status_code == 200, "4: signed in again with a new link"
    again = app.test_client()
    r = again.get(link)
    assert r.status_code == 302 and "/login" in r.headers["Location"] and again.get("/").status_code == 302, \
        "4: HARD FAIL - the link worked twice"
    assert anon.post("/api/jarvis/logout", headers=h).status_code == 204
    r = tab.get("/")
    assert r.status_code == 302 and "/login" in r.headers["Location"], "4: HARD FAIL - the tab outlived Jarvis's sign-in"


def app_db_dir(app):
    import app.db as app_db
    return Path(app_db.DB_PATH).parent


def selftest():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        app, conn, db_path, token, other = setup(tmp)
        staff_client(app, db_path, "dbianchi", "dentist")
        from werkzeug.security import generate_password_hash
        conn.execute("UPDATE users SET password_hash = ?, must_change_password = 0 WHERE username = 'dbianchi'",
                     (generate_password_hash(PASSWORD),))
        conn.commit()
        device = jl.authenticate(conn, token)
        when = read(conn, device, other)
        edit(app, db_path, conn, device, other, token)
        handoff(app, db_path, conn, device, other, token)
        http(app, conn, device, token, when)
        raw = "\n".join(str(tuple(x)) for x in conn.execute("SELECT * FROM audit_log"))
        assert PASSWORD not in raw, "HARD FAIL - a password in the audit"
        conn.close()
    print(json.dumps({"jarvis_patients_selftest": "ok"}))


if __name__ == "__main__":
    selftest()
    sys.exit(0)
