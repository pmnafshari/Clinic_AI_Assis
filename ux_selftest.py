"""P23: the staff shell and the five redesigned pages, as behaviour. written before the redesign.

a temporary synthetic clinic, the real app through its test client, a pinned clinic clock that is deliberately a
different day from the machine's (so a page that reads the server date instead of the clinic's shows the wrong day).
"""

import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from werkzeug.security import generate_password_hash

import appointments
import clinic_time
import demo_fixtures
import patient_id
import storage

TODAY = datetime(2031, 3, 5, 9, 0)            # a Wednesday, and not the machine's date
DAY = "2031-03-05"
NAV = {
    "dentist": {"/", "/appointments", "/patients", "/reviews", "/qa", "/notes/new", "/billing", "/stock",
                "/handoffs", "/reminders", "/data-requests", "/reports", "/providers"},
    "assistant": {"/", "/appointments", "/patients", "/qa", "/notes/new", "/billing", "/stock", "/handoffs",
                  "/reminders", "/providers"},
    "admin": {"/admin/users", "/patients/duplicates"},
}
CF_SHAPE = re.compile(r"\b[A-Z]{6}[0-9LMNPQRSTUV]{2}[A-Z][0-9LMNPQRSTUV]{2}[A-Z][0-9LMNPQRSTUV]{3}[A-Z]\b|\b[A-Z]{4}[0-9]{12}\b")


def _text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def _section(html, marker):
    m = re.search(rf'<[^>]+data-ux="{marker}"[^>]*>(.*?)<!--/{marker}-->', html, re.S)
    assert m, f"no data-ux={marker} section"
    return m.group(1)


def _client(app, user, password):
    c = app.test_client()
    r = c.post("/login", data={"username": user, "password": password})
    assert r.status_code in (302, 303), f"{user} could not sign in"
    return c


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "ux.sqlite"
        conn = storage.init_db(str(db))
        demo_fixtures.seed(conn)
        for user, role in (("dentist", "dentist"), ("drbianchi", "dentist"), ("assistant", "assistant"), ("admin", "admin")):
            conn.execute("INSERT INTO users (username, password_hash, role, active) VALUES (?, ?, ?, 1)",
                         (user, generate_password_hash(f"{user}-pw-123"), role))
        conn.execute("INSERT OR IGNORE INTO dentist_schedule (dentist, weekday, starts, ends) VALUES ('drbianchi', 2, '09:00', '18:00')")
        anna = patient_id.seed_patient(conn, "ZZUA800101010101", "Anna Uno")
        bea = patient_id.seed_patient(conn, "ZZUB800101010102", "Bea Due", "+393401112233")
        carlo = patient_id.seed_patient(conn, "ZZUC800101010103", "Carlo Tre")
        patient_id.seed_patient(conn, "ZZUD800101010104", "Dino Quattro")      # no booking, no request
        conn.execute("INSERT INTO visits (patient_id, visit_date, procedures, clinical_notes, next_appointment, source_path)"
                     " VALUES (?, '2031-02-01', '[]', 'controllo', '2031-04-01', 'sorted/ZZUA800101010101/notes/a.json')", (anna,))
        conn.commit()
        appointments.book(conn, anna, "dentist", f"{DAY}T09:30", 30)
        appointments.book(conn, bea, "drbianchi", f"{DAY}T10:00", 30)
        appointments.book(conn, bea, "dentist", "2031-03-07T11:00", 30)            # same week, Friday
        cx = appointments.book(conn, carlo, "dentist", f"{DAY}T15:00", 30)
        appointments.cancel(conn, cx)
        appointments.request(conn, carlo, "2031-03-10", "morning", "controllo")
        conn.commit()

        import app.db as app_db
        from app import create_app
        app_db.DB_PATH = str(db)
        app = create_app()
        app.config["WTF_CSRF_ENABLED"] = False
        clients = {u: _client(app, u, f"{u}-pw-123") for u in ("dentist", "assistant", "admin")}

        # 1. the sidebar lists exactly the role's authorised pages, and every link opens
        for role, client in clients.items():
            page = client.get("/admin/users" if role == "admin" else "/").get_data(as_text=True)
            nav = _section(page, "nav")
            hrefs = set(re.findall(r'<a[^>]*class="[^"]*app-side-link[^"]*"[^>]*href="([^"?#]+)', nav)) | \
                set(re.findall(r'<a[^>]*href="([^"?#]+)"[^>]*class="[^"]*app-side-link', nav))
            assert hrefs == NAV[role], f"1: {role} nav {sorted(hrefs)} != {sorted(NAV[role])}"
            for href in hrefs:
                r = client.get(href)
                assert r.status_code == 200, f"1: {role} nav link {href} gave {r.status_code}"

        # 2. sign out: visible in the sidebar footer, a POST form, apart from change password
            foot = _section(page, "account")
            assert re.search(r'<form[^>]*method="post"[^>]*action="/logout"', foot), f"2: {role} sign out is not a POST form"
            assert re.search(r'class="[^"]*app-signout[^"]*"[^>]*>\s*(<i[^>]*></i>\s*)?Sign out', foot), f"2: {role} no Sign out button"
            assert "Logout" not in page and "Change password" in foot

        dentist, assistant = clients["dentist"], clients["assistant"]

        # 2b. a page left out of a role's sidebar is also refused at its route, not only hidden
        for path in ("/reviews", "/reports", "/data-requests", "/admin/users"):
            assert assistant.get(path).status_code in (302, 403), f"2b: assistant reached {path}"
        for path in ("/", "/appointments", "/patients", "/notes/new"):
            assert clients["admin"].get(path).status_code in (302, 403), f"2b: admin reached {path}"
        refused = clients["admin"].get("/qa").get_data(as_text=True)      # refuses in-page, and audits it
        assert "permission" in refused and "data-example" not in refused and 'name="question"' not in refused, \
            "2b: admin is shown the records question form"

        # 3. one primary action per redesigned page
        for path in ("/", f"/appointments?day={DAY}", "/patients", "/qa", "/notes/new"):
            html = dentist.get(path).get_data(as_text=True)
            head = _section(html, "page-head")
            n = len(re.findall(r'class="[^"]*\bapp-primary\b', head))
            assert n <= 1, f"3: {path} has {n} primary actions"

        # 4. home: the clinic's today, plain-words activity, no paths or codici fiscali
        home = dentist.get("/").get_data(as_text=True)
        agenda = _text(_section(home, "agenda"))
        assert "09:30" in agenda and "Anna Uno" in agenda and "10:00" in agenda, f"4: clinic-day agenda missing: {agenda[:200]}"
        assert "15:00" not in agenda, "4: a cancelled appointment on the agenda"
        activity = _section(home, "activity")
        assert "sorted/" not in activity and ".json" not in activity and not CF_SHAPE.search(_text(activity)), "4: raw activity"
        assert "1 request" in _text(_section(home, "attention")), "4: the waiting request is not in the attention line"

        # 4b. activity times are the clinic's (the undo log stores UTC; 08:15 UTC in March is 09:15 in Rome)
        import app.dashboard_routes as dr
        log = Path(tmp) / "undo.jsonl"
        log.write_text('{"ts": "2031-03-05T08:15:00+00:00", "tool": "update_field", "codice_fiscale": "ZZUA800101010101",'
                       ' "target": "sqlite:patients.phone", "before": "x", "username": "dentist"}\n')
        with log.open("a") as f:
            f.write('{"ts": "2031-03-04T23:30:00", "tool": "update_field", "codice_fiscale": "ZZUA800101010101",'
                    ' "target": "sqlite:patients.phone", "before": "x", "username": "dentist"}\n')
        real_log, dr.UNDO_LOG = dr.UNDO_LOG, str(log)
        try:
            with app.app_context():
                acts = dr._activity(conn, "dentist")
        finally:
            dr.UNDO_LOG = real_log
        assert [a["when"] for a in acts] == ["5 Mar, 09:15", "2031-03-04"] and acts[0]["who"] == "Anna Uno", f"4b: {acts}"

        # 5. appointments: day by clinician, week, month, filters, requests apart, booking panel on demand
        day = dentist.get(f"/appointments?view=day&day={DAY}").get_data(as_text=True)
        grid = _section(day, "day-grid")
        assert "drbianchi" in grid and "dentist" in grid, "5: clinician columns missing"
        assert "Anna Uno" in grid and "Bea Due" in grid and "Carlo Tre" not in grid, "5: day grid shows the wrong rows"
        reqs = _text(_section(day, "requests"))
        assert "Carlo Tre" in reqs and "Requested" in reqs and "not bookings" in reqs.lower(), "5: requests panel"
        assert 'id="book"' in day, "5: the booking panel (#book) is gone"
        r = dentist.get("/appointments?view=day&day=2031-03-10&status=all")
        assert r.status_code == 200, f"5: the day holding a request fails to draw ({r.status_code})"
        on_pref = r.get_data(as_text=True)
        assert "Carlo Tre" not in _section(on_pref, "day-grid") and "Carlo Tre" in _section(on_pref, "requests"), \
            "5: a request is drawn in the grid as if it were a booking"
        filtered = _section(dentist.get(f"/appointments?view=day&day={DAY}&dentist=drbianchi").get_data(as_text=True), "day-grid")
        assert "Bea Due" in filtered and "Anna Uno" not in filtered, "5: the clinician filter does nothing"
        cancelled = _text(_section(dentist.get(f"/appointments?view=day&day={DAY}&status=cancelled").get_data(as_text=True), "day-grid"))
        assert "Carlo Tre" in cancelled and "Cancelled" in cancelled and "Anna Uno" not in cancelled, "5: the status filter"
        week = _text(_section(dentist.get(f"/appointments?view=week&day={DAY}").get_data(as_text=True), "week"))
        assert "Anna Uno" in week and "11:00" in week and "Bea Due" in week, "5: week view"
        month = dentist.get(f"/appointments?view=month&day={DAY}").get_data(as_text=True)
        assert "March 2031" in month and 'data-ux="month"' in month, "5: month view"
        for view in ("day", "week"):
            html = dentist.get(f"/appointments?view={view}&day={DAY}").get_data(as_text=True)
            assert not CF_SHAPE.search(_text(_section(html, "day-grid" if view == "day" else "week"))), \
                f"5: a codice fiscale in the {view} view"

        # 6. patients: sortable, explicit missing values, next booking with its status
        pl = dentist.get("/patients?sort=next").get_data(as_text=True)
        rows = re.findall(r'<tr[^>]*data-patient="([^"]+)"', pl)
        assert rows[:2] == [anna, bea], f"6: sort by next appointment gave {rows}"
        text = _text(pl)
        assert "No phone" in text and "No booking" in text, "6: missing values are not explicit"
        assert "Booked" in text and f"{DAY} 09:30" in text, "6: next booking without date/time/status"
        assert "Requested 2031-03-10" in text, "6: a waiting request is not shown as requested"
        assert "Anna Uno" in _text(dentist.get("/patients?q=anna").get_data(as_text=True)) and \
            "Bea Due" not in _text(_section(dentist.get("/patients?q=anna").get_data(as_text=True), "patient-rows")), "6: search"

        # 7. q&a: examples and scope; next appointment from the booking (the surface P22 missed); no record
        qa = _text(dentist.get("/qa").get_data(as_text=True))
        assert "Examples" in qa and "only patients you may see" in qa.lower(), "7: no examples or scope"
        ans = _text(dentist.post("/qa", data={"question": "What is patient Anna Uno's next appointment?"}).get_data(as_text=True))
        assert f"{DAY} 09:30" in ans and "2031-04-01" not in ans.split("recall")[0], f"7: answered from the recall: {ans[-300:]}"
        assert "sorted/" not in ans and ".json" not in ans, "7: a raw path cited as the source"
        none = _text(dentist.post("/qa", data={"question": "What is patient Zeno Nessuno's phone number?"}).get_data(as_text=True))
        assert "No patient" in none, "7: the no-record state"

        # 7b. every example the page offers is one the records can answer (a name and a field, or a search question)
        import ask
        examples = re.findall(r'data-example="([^"]+)"', dentist.get("/qa").get_data(as_text=True))
        assert len(examples) >= 4, examples
        for ex in (e.replace("&#39;", "'") for e in examples):
            ok = ask.classify_question(ex) == "meaning" or (ask.extract_name(ex) and ask.field_for_question(ex))
            assert ok, f"7b: the example {ex!r} cannot be answered"

        # 8. add note: stages, draft vs confirmed, recall label, unsaved-change guard
        import app.notes_routes as nr
        from dental_notes_schema import DentalNote
        nr.extract_note = lambda raw, fallback_cf=None: DentalNote(patient_name="Anna Uno", codice_fiscale="ZZUA800101010101",
                                                                   procedures=["prophy"], clinical_notes="igiene")
        blank = dentist.get("/notes/new").get_data(as_text=True)
        assert 'data-step="1" aria-current="step"' in blank and "data-unsaved-guard" in blank, "8: stages or guard missing"
        draft = dentist.post("/notes/new", data={"raw_note": "igiene fatta"}).get_data(as_text=True)
        assert 'data-step="2" aria-current="step"' in draft and "Draft - not saved" in _text(draft), "8: draft state not shown"
        assert re.search(r'<form[^>]*data-unsaved-guard[^>]*>(?:(?!</form>).)*name="confirm_token"', draft, re.S), \
            "8: leaving an edited draft does not ask first"
        assert "Recall" in _text(draft) and ">Next appointment<" not in draft, "8: the recall field is labelled as an appointment"
        for tpl in ("_visit_edit_form.html", "review_detail.html", "notes_new.html"):
            src = (Path(__file__).parent / "app" / "templates" / tpl).read_text()
            assert "Recall, as written in the note" in src and not re.search(r">\s*Next appointment[^<]*</label>", src), \
                f"8: {tpl} labels the recall as an appointment"

        # 10. P23 follow-up (owner visual review): the shell, Home, Patients and Billing as behaviour.
        # 10a. the sidebar count says what it counts - pending requests on their own entry, not on Appointments
        nav = _section(dentist.get("/").get_data(as_text=True), "nav")
        appt_link = re.search(r'<a[^>]*href="/appointments"[^>]*>.*?</a>', nav, re.S).group(0)
        assert "app-side-count" not in appt_link, "10a: a count sits on Appointments, where it reads as appointments"
        req_link = re.search(r'<a[^>]*href="/appointments#requests"[^>]*>.*?</a>', nav, re.S)
        assert req_link and "Requests" in req_link.group(0) and ', 1 pending request</span>' in req_link.group(0), \
            f"10a: no labelled Requests entry: {req_link and req_link.group(0)}"
        # 10b. home: every agenda row opens the patient's record; alerts sit apart; the quiet day offers the next booked day
        home = dentist.get("/").get_data(as_text=True)
        agenda = _section(home, "agenda")
        assert agenda.count('href="/patients/ZZUA800101010101"') == 1 and ">Open" in agenda, "10b: agenda rows have no Open"
        assert 'data-ux="alerts"' in home and 'class="ux-kpi-icon' in home, "10b: no alerts card or compact KPI"
        real_now = clinic_time.now
        clinic_time.now = lambda env=None: datetime(2031, 3, 4, 9, 0)        # a day with nothing booked
        try:
            quiet = _section(dentist.get("/").get_data(as_text=True), "agenda")
        finally:
            clinic_time.now = real_now
        assert "Nothing booked today" in quiet and f"day={DAY}" in quiet and "5 Mar" in _text(quiet), \
            f"10b: the quiet day does not offer the next booked day: {_text(quiet)}"
        # 10c. patients: one list filter, one record lookup, differently named; no page action; no inner scroll;
        # the codice fiscale shortened in the list and still searchable in full
        pl = dentist.get("/patients").get_data(as_text=True)
        assert len(re.findall(r'class="[^"]*\bapp-primary\b', _section(pl, "page-head"))) == 0, "10c: page-level Add note"
        assert "Filter this list" in pl and 'aria-label="Go to a patient record"' in pl, "10c: two searches, one name"
        assert "table-responsive" not in pl and "ux-scroll" not in pl, "10c: the table scrolls inside the page"
        rows = _section(pl, "patient-rows")
        assert "ZZUA800101010101" not in _text(rows) and "ZZUA…0101" in _text(rows), "10c: full codice fiscale in the list"
        assert 'href="/patients/ZZUA800101010101"' in rows, "10c: the row no longer opens the record"
        found = _section(dentist.get("/patients?q=ZZUB800101010102").get_data(as_text=True), "patient-rows")
        assert "Bea Due" in found and "Anna Uno" not in found, "10c: a full codice fiscale no longer finds the patient"
        # 10d. billing: three grouped tables, the same columns, amounts unchanged, actions only where the role may act
        import ledger
        def visit(pid, day, cents):
            vid = conn.execute("INSERT INTO visits (patient_id, visit_date, procedures, clinical_notes, source_path)"
                               " VALUES (?, ?, '[]', '', ?)", (pid, day, f"b/{pid}{day}")).lastrowid
            conn.execute("INSERT INTO invoices (patient_id, visit_id, line_index, amount, amount_cents, description)"
                         " VALUES (?, ?, 0, ?, ?, 'visita')", (pid, vid, cents / 100, cents))
            return vid
        owed_id = ledger.ensure_invoice(conn, anna, visit(anna, "2031-02-10", 12000))
        ledger.issue(conn, owed_id, "dentist", "dentist", "2031-03-20")
        ledger.record_payment(conn, owed_id, "20,00", "cash", "k-ux-1", "dentist", "dentist", "2031-02-11")
        unknown_id = ledger.ensure_invoice(conn, bea, visit(bea, "2031-01-15", 8000), legacy=True)
        draft_id = ledger.ensure_invoice(conn, carlo, visit(carlo, "2031-02-20", 5000))
        conn.commit()
        bills = {}
        for who, client in (("dentist", dentist), ("assistant", assistant)):
            bills[who] = client.get("/billing").get_data(as_text=True)
        for key, pid, inv in (("owed", anna, owed_id), ("unknown", bea, unknown_id), ("draft", carlo, draft_id)):
            sec = _section(bills["dentist"], f"billing-{key}")
            assert "<table" in sec and all(h in sec for h in (">Patient<", ">Invoice<", ">Due<", ">Amount<", ">Status<")), \
                f"10d: {key} is not the shared table"
            assert f"#{inv}" in _text(sec), f"10d: {key} row has no invoice reference"
        owed = _text(_section(bills["dentist"], "billing-owed"))
        assert "Anna Uno" in owed and "€100.00" in owed and "€120.00" in owed and "2031-03-20" in owed, f"10d: {owed}"
        head = _text(_section(bills["dentist"], "billing-totals"))
        assert "€100.00" in head and "€20.00" in head, f"10d: drafts or unknowns counted as owed: {head}"
        assert "Bea Due" not in owed and "Carlo Tre" not in owed, "10d: an unknown or draft invoice listed as owed"
        assert ">Issue<" in _section(bills["dentist"], "billing-draft") and ">Reconcile<" in _section(bills["dentist"], "billing-unknown")
        for key in ("draft", "unknown"):
            sec = _section(bills["assistant"], f"billing-{key}")
            assert ">Issue<" not in sec and ">Reconcile<" not in sec and ">View<" in sec, f"10d: assistant offered a {key} action"
        assert ">Record payment<" in _section(bills["assistant"], "billing-owed"), "10d: reception cannot reach record payment"
        assert f'href="/patients/ZZUA800101010101/billing#invoice-{owed_id}"' in bills["assistant"], "10d: action goes nowhere"
        detail = dentist.get("/patients/ZZUA800101010101/billing").get_data(as_text=True)
        assert f'id="invoice-{owed_id}"' in detail, "10d: the action's anchor is missing on the patient page"
        assert "no fiscal invoice" in _text(bills["assistant"]), "10d: the demo/manual-payment explanation is gone"
        # 10e. the staff palette the owner specified
        css = (Path(__file__).parent / "app" / "static" / "css" / "app.css").read_text().lower()
        for decl in ("--app-navy: #142036;", "--app-navy-active: #294781;", "--ds-ground: #f6f8fc;",
                     "--ds-primary: #2453d4;", "--ds-border: #d9e1ec;",
                     ".app-side.offcanvas-lg { background-color: var(--app-navy) !important; }",
                     ".app-side-link.active { background: var(--app-navy-active); color: #fff;"):
            assert decl in css, f"10e: the staff palette lost {decl!r}"

        # 9. the demo seed's tags carry no codice fiscale
        import demo_seed
        demo_seed.seed(conn, seed=22, anchor="2031-03-10")
        tags = [r[0] for r in conn.execute("SELECT note FROM appointments WHERE note LIKE 'Demo %'")]
        assert tags and not any(CF_SHAPE.search(t) for t in tags), f"9: seed tags {tags[:2]}"
        assert not conn.execute("SELECT 1 FROM appointments WHERE note LIKE 'demo-seed:%'").fetchone(), "9: an old tag remains"
        conn.close()
    print("selftest ok")


def main():
    if "--selftest" not in sys.argv:
        print("usage: python ux_selftest.py --selftest")
        return
    real = clinic_time.now
    clinic_time.now = lambda env=None: TODAY
    try:
        selftest()
    finally:
        clinic_time.now = real


if __name__ == "__main__":
    main()
