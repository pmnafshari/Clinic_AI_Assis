"""P23: the staff shell and the five redesigned pages, as behaviour. written before the redesign.

a temporary synthetic clinic, the real app through its test client, a pinned clinic clock that is deliberately a
different day from the machine's (so a page that reads the server date instead of the clinic's shows the wrong day).
"""

import json
import re
import sys
import tempfile
from datetime import datetime
from html.parser import HTMLParser
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
    # /imports (P25): the dentist reviews, reception sees progress only; admin never
    # /guides/ask (P24): dentist and reception; admin never
    # /imaging/tasks (P27): dentist and reception; admin never
    # /jarvis/link (Jarvis J00): dentist and reception delegate their own session; admin registers devices instead
    # /tasks (NEW-04): dentist and reception; admin never
    # /aftercare (NEW-05): the dentist's alone - clinical content
    "dentist": {"/", "/appointments", "/requests", "/patients", "/reviews", "/imports", "/imaging/tasks", "/qa", "/guides/ask", "/jarvis/link", "/tasks", "/notes/new", "/billing", "/stock",
                "/handoffs", "/reminders", "/data-requests", "/aftercare", "/outreach", "/reports", "/providers"},
    "assistant": {"/", "/appointments", "/requests", "/patients", "/imports", "/imaging/tasks", "/qa", "/guides/ask", "/jarvis/link", "/tasks", "/notes/new", "/billing", "/stock",
                  "/handoffs", "/reminders", "/outreach", "/providers"},
    "admin": {"/admin/users", "/patients/duplicates", "/jarvis/devices"},
}
CF_SHAPE = re.compile(r"\b[A-Z]{6}[0-9LMNPQRSTUV]{2}[A-Z][0-9LMNPQRSTUV]{2}[A-Z][0-9LMNPQRSTUV]{3}[A-Z]\b|\b[A-Z]{4}[0-9]{12}\b")


def _text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def _section(html, marker):
    m = re.search(rf'<[^>]+data-ux="{marker}"[^>]*>(.*?)<!--/{marker}-->', html, re.S)
    assert m, f"no data-ux={marker} section"
    return m.group(1)


class _Lists(HTMLParser):
    """every [data-list-tools] on a page, and the rows (direct children) of each tbody/ul that has an id."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

    def __init__(self):
        super().__init__()
        self.tools, self.rows, self._stack = [], {}, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "data-list-tools" in a:
            self.tools.append(a)
        parent = self._stack[-1] if self._stack else (None, None)
        if tag in ("tr", "li") and parent[0] in ("tbody", "ul") and parent[1]:
            self.rows[parent[1]].append(a)
        if tag in self.VOID:
            return
        if tag in ("tbody", "ul") and a.get("id"):
            self.rows[a["id"]] = []
        self._stack.append((tag, a.get("id")))

    def handle_endtag(self, tag):
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                del self._stack[i:]
                break


def _lists(html):
    parser = _Lists()
    parser.feed(html)
    return parser.tools, parser.rows


def assert_list_tools(html, where, keys):
    """every list tool on a page works on rows that can answer it: each sort key on every row, each row's filter value
    reachable by a choice, a noun for the count - and the page offers at least the sorts named in keys."""
    tools, rows = _lists(html)
    assert tools, f"14a: {where} has no list tools"
    seen = set()
    for tool in tools:
        target = tool["data-for"]
        assert rows.get(target), f"14a: {where} {target} has no rows to work on"
        sorts = json.loads(tool["data-sorts"])
        filters = json.loads(tool.get("data-filters") or "[]")
        assert sorts and all(len(x) == 4 and x[1] in ("text", "num") and x[2] in (1, -1) for x in sorts), f"14a: {where} sorts {sorts}"
        for key, _kind, _dir, label in sorts:
            seen.add(key)
            assert all(f"data-{key}" in r for r in rows[target]), f"14a: {where} {target}: a row has no data-{key} ({label})"
        for key, _group, options in filters:
            assert all(f"data-{key}" in r for r in rows[target]), f"14a: {where} {target}: a row has no data-{key}"
            held = {r.get(f"data-{key}") for r in rows[target]}
            assert held <= {v for v, _l in options}, f"14a: {where} {target}: a {key} value no choice reaches: {held}"
        assert "|" in tool["data-noun"], f"14a: {where} {target} has no noun for its count"
    assert set(keys) <= seen, f"14a: {where} is missing sorts {set(keys) - seen}"


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
        # the sidebar walk opens Ask clinic guides (P24): its store goes to this temp folder, never db/
        import clinic_guides
        clinic_guides.DB_PATH = str(Path(db).parent / "guides.sqlite")
        clinic_guides.STORE = Path(db).parent / "guides"
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
        for path in ("/", "/appointments", "/patients", "/notes/new", "/imports", "/guides/ask", "/guides"):
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

        # 4c. P29 F-019: an upload shows its latest state - the dentist's decision on it - and a
        # refused type says why instead of "no patient matched"
        import note_review as _nr
        from auth import log_audit as _log
        empty = Path(tmp) / "empty-undo.jsonl"
        empty.write_text("")
        for name, status, why in (("ok.txt", "confirmed", None), ("no.txt", "rejected", "wrong patient")):
            conn.execute("INSERT INTO note_reviews (origin, status, original_name, extraction, created_by,"
                         " created_at, decided_by, decided_at, decision_reason) VALUES ('upload', ?, ?, '{}',"
                         " 'dentist', '2031-03-05T08:00:00+00:00', 'drbianchi', '2031-03-05T09:00:00+00:00', ?)",
                         (status, name, why))
            _log(conn, "dentist", "dentist", "note_staged", f"sorted/ZZUA800101010101/notes/{name}", allowed=1,
                 reason="awaiting_review")
        _log(conn, "dentist", "dentist", "upload_file", "scan.heic", allowed=0)
        conn.commit()
        real_log, dr.UNDO_LOG = dr.UNDO_LOG, str(empty)
        try:
            with app.app_context():
                acts = {a["text"]: a["who"] for a in dr._activity(conn, "dentist", limit=10)}
        finally:
            dr.UNDO_LOG = real_log
        assert "Note confirmed by drbianchi" in acts, f"4c: the confirmed upload still shows its first state {acts}"
        assert "Note rejected by drbianchi: wrong patient" in acts, f"4c: the rejection and its reason {acts}"
        assert acts.get("Upload refused - this file type is not accepted") == "scan.heic", \
            f"4c: a refused type still reads as an unmatched patient {acts}"

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
        # P29 R-009 / R-010: cancel names what it cancels and asks first; Show says when filters are not applied
        dayhtml = dentist.get(f"/appointments?view=day&day={DAY}").get_data(as_text=True)
        assert re.search(r'data-confirm-cancel="Anna Uno · [^"]+ · \d\d:\d\d–\d\d:\d\d · ', dayhtml), \
            "5 R-009: the cancel form does not name the appointment"
        assert re.search(r'<dialog[^>]+id="cancel-dialog"', dayhtml) and "I am sure" in dayhtml \
            and re.search(r'data-cancel-yes disabled', dayhtml), "5 R-009: no confirm dialog with a held button"
        assert "data-apply-button" in dayhtml and "Filters changed - press Show to apply" in dayhtml, \
            "5 R-010: Show does not mark unapplied filters"
        js = (Path(__file__).parent / "app" / "static" / "js" / "app.js").read_text()
        # P29 U-007 / R-021: limited fields count, say when full, and the short-answer boxes grow
        assert "P29 U-007" in js and "limit reached" in js and "data-grow" in js, "5 U-007: no counter code"
        imaging_t = (Path(__file__).parent / "app" / "templates" / "imaging_request.html").read_text()
        assert '<textarea id="rnote-b"' in imaging_t and "data-grow" in imaging_t, "5 U-007: 'in a few words' is one line"
        appt_t = (Path(__file__).parent / "app" / "templates" / "appointments.html").read_text()
        reason_t = (Path(__file__).parent / "app" / "templates" / "_reason.html").read_text()
        assert "reason_box('dreason-'" in appt_t and re.search(r'<textarea[^>]+name="reason"[^>]+data-grow', reason_t), \
            "5 U-007: the decline reason is one line"
        cancel_js = js[js.index("P29 R-009"):js.index("P29 R-010")]
        assert cancel_js.count("HTMLFormElement.prototype.submit") == 1 and \
            cancel_js.index("HTMLFormElement.prototype.submit") > cancel_js.index('yes.addEventListener("click"'), \
            "5 R-009: something other than the Yes click submits the cancel"
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
        # P29 F-005 / R-004: a second click on the active column reverses it, and says so
        flipped = dentist.get("/patients?sort=next&dir=desc").get_data(as_text=True)
        frows = re.findall(r'<tr[^>]*data-patient="([^"]+)"', flipped)
        assert frows.index(bea) < frows.index(anna), f"6 F-005: dir=desc did not reverse {frows}"
        assert re.search(r'<th scope="col" aria-sort="ascending"><a class="sx-th" href="[^"]*sort=next[^"]*dir=desc', pl), \
            "6 F-005: the active column does not offer the reverse order"
        assert re.search(r'<th scope="col" aria-sort="descending"><a class="sx-th" href="[^"]*sort=next[^"]*dir=asc', flipped), \
            "6 F-005: and back again"
        assert pl.count("bi-chevron-expand") == 2, "6 R-004: inactive sortable columns show no sort cue"
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
        # P29 F-039: the full name without "patient" is the same question
        bare = _text(dentist.post("/qa", data={"question": "When is Anna Uno's next appointment?"}).get_data(as_text=True))
        assert f"{DAY} 09:30" in bare, f"7: a full name without 'patient' was not recognised: {bare[-300:]}"
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
        # P29 R-028: leaving through a link asks in the app's dialog; close/reload keep the browser's own warning
        assert re.search(r'<dialog[^>]+id="leave-dialog"', blank) and "Leave without saving" in blank, \
            "8 R-028: no in-app leave dialog"
        app_js = (Path(__file__).parent / "app" / "static" / "js" / "app.js").read_text()
        guard = app_js[app_js.index("P29 R-028"):]
        assert "beforeunload" in guard and "leave-dialog" in guard, "8 R-028: the guard is not in app.js"
        draft = dentist.post("/notes/new", data={"raw_note": "igiene fatta"}, follow_redirects=True).get_data(as_text=True)
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
        req_link = re.search(r'<a[^>]*href="/requests"[^>]*>.*?</a>', nav, re.S)
        assert req_link and "Requests" in req_link.group(0) and ', 1 pending request</span>' in req_link.group(0), \
            f"10a: no labelled Requests entry: {req_link and req_link.group(0)}"
        # 10b. home: every agenda row opens the patient's record; alerts sit apart; the quiet day offers the next booked day
        home = dentist.get("/").get_data(as_text=True)
        agenda = _section(home, "agenda")
        assert agenda.count('href="/patients/ZZUA800101010101"') == 1 and ">Open" in agenda, "10b: agenda rows have no Open"
        # (the follow-up's alerts card was rejected by the owner in correction 2 - 11j asserts it is gone)
        assert 'class="ux-kpi-icon' in home, "10b: no compact KPI"
        real_now = clinic_time.now
        clinic_time.now = lambda env=None: datetime(2031, 3, 4, 9, 0)        # a day with nothing booked
        try:
            quiet = _section(dentist.get("/").get_data(as_text=True), "agenda")
        finally:
            clinic_time.now = real_now
        assert "Nothing booked today" in quiet and f"day={DAY}" in quiet and "5 Mar" in _text(quiet), \
            f"10b: the quiet day does not offer the next booked day: {_text(quiet)}"
        # 10c. patients: one list filter, one record lookup, differently named; no page action; the table in a scroll box (P30:
        # the owner now wants every table and box a fixed size that scrolls inside - 15c asserts the box);
        # the codice fiscale shortened in the list and still searchable in full
        pl = dentist.get("/patients").get_data(as_text=True)
        # P29 E-001: the one page action is New patient, for walk-ins; Add note stays on the record
        heads10 = re.findall(r'<a class="[^"]*\bapp-primary\b[^"]*" href="([^"]+)"[^>]*>([^<]+)<', _section(pl, "page-head"))
        assert heads10 == [("/patients/new", "New patient")], f"10c: page actions {heads10}"
        assert "Filter this list" in pl and 'aria-label="Go to a patient record"' in pl, "10c: two searches, one name"
        assert "table-responsive" not in pl and "ux-scroll" not in pl, "10c: the table scrolls inside the page"
        rows = _section(pl, "patient-rows")
        assert "ZZUA800101010101" not in _text(rows) and "ZZUA…0101" in _text(rows), "10c: full codice fiscale in the list"
        assert 'href="/patients/ZZUA800101010101"' in rows, "10c: the row no longer opens the record"
        # P29 F-004 (owner 2026-11-03): a codice fiscale matches only when it is typed whole
        part = _section(dentist.get("/patients?q=ZZUB80").get_data(as_text=True), "patient-rows")
        assert 'data-patient="' not in part and "No patient matches" in part, "F-004: part of a code still matches"
        whole_lower = _section(dentist.get("/patients?q=zzub 8001 0101 0102").get_data(as_text=True), "patient-rows")
        assert 'data-patient="' in whole_lower, "F-004: the whole code typed loosely finds nobody"
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

        # 11. P23 correction 2 (owner's annotated assistant screens of fda69e4)
        A = assistant
        def appt(view, day=DAY, **kw):
            q = "&".join(f"{k}={v}" for k, v in kw.items())
            return A.get(f"/appointments?view={view}&day={day}" + (f"&{q}" if q else "")).get_data(as_text=True)
        # 11a. no timezone text in the top bar; one labelled clinic-time cue per page
        for path in ("/", f"/appointments?view=day&day={DAY}"):
            page = A.get(path).get_data(as_text=True)
            top = re.search(r'<header class="app-topbar".*?</header>', page, re.S).group(0)
            assert "Europe/Rome" not in top and "app-topbar-zone" not in top, f"11a: {path} top bar still shows the zone"
            assert _text(page).count("clinic time") == 1, f"11a: {path} has {_text(page).count('clinic time')} clinic-time cues"
        # 11b. Day, Week and Month count the same confirmed bookings, with and without a clinician filter
        def summary(html):
            return _text(_section(html, "appt-summary")).strip()
        assert "2 confirmed bookings" in summary(appt("day")), summary(appt("day"))
        assert "3 confirmed bookings" in summary(appt("week")), summary(appt("week"))
        assert "3 confirmed bookings" in summary(appt("month")), summary(appt("month"))
        assert "1 confirmed booking" in summary(appt("day", dentist="drbianchi"))
        assert "1 confirmed booking" in summary(appt("week", dentist="drbianchi"))
        month_b = appt("month", dentist="drbianchi")
        def cell(html, iso):
            # the visible count is drawn from data-text (label in name), so that is what is on screen
            tag = re.search(r'<a class="appt-cal-day[^>]*\bday=' + iso + r'&[^>]*>(.*?)</a>', html, re.S)
            return re.search(r'aria-label="([^"]*)"', tag.group(0)).group(1), " ".join(re.findall(r'data-text="([^"]*)"', tag.group(1)))
        assert cell(month_b, "2031-03-05")[0] == "5 March 2031: 1 booked", cell(month_b, "2031-03-05")
        assert cell(month_b, "2031-03-07")[0] == "7 March 2031: nothing booked", "11b: month ignores the clinician filter"
        label, shown = cell(appt("month"), "2031-03-05")
        assert label == "5 March 2031: 2 booked" and "2 booked" in shown, f"11b: month count not visible: {shown!r}"
        assert "1 request" in cell(appt("month"), "2031-03-10")[1], "11b: a preferred day is not marked as a request"
        # 11c. the controls sit in one place and switching views keeps the date and the filters
        for view in ("day", "week", "month"):
            html = appt(view, dentist="drbianchi", status="all")
            bar = _section(html, "appt-controls")
            for other in ("day", "week", "month"):
                link = re.search(rf'<a[^>]*class="ux-tab[^"]*"[^>]*href="([^"]*view={other}[^"]*)"', bar).group(1)
                assert f"day={DAY}" in link and "dentist=drbianchi" in link and "status=all" in link, f"11c: {view}->{other} {link}"
            assert re.search(r'<input[^>]*type="date"[^>]*id="day"', bar) and ">Today<" in bar and 'rel="prev"' in bar \
                and 'rel="next"' in bar, f"11c: {view} lost a control"
            assert "appt-cal-nav" not in html.split('data-ux="appt-controls"')[1].split("<!--/appt-controls-->")[1], \
                f"11c: {view} has a second date navigation outside the controls"
        # 11d. empty states: no empty hour rows, no seven repeated messages, a route to the next confirmed booking
        empty_day = appt("day", day="2031-03-06")
        grid = _section(empty_day, "day-grid")
        assert "<table" not in grid and "No confirmed bookings" in _text(grid), "11d: an empty day draws hour rows"
        assert 'href="/appointments?view=day&amp;day=2031-03-07' in grid and "Fri 7 Mar" in _text(grid), \
            f"11d: no route to the next confirmed booking: {_text(grid)}"
        empty_week = _section(appt("week", day="2031-02-24"), "week")
        assert _text(empty_week).count("Nothing") + _text(empty_week).count("No confirmed") <= 1, "11d: repeated empty messages"
        assert "day=2031-03-05" in empty_week and "Wed 5 Mar" in _text(empty_week), "11d: empty week has no next booking"
        full_day = _section(appt("day"), "day-grid")
        hours = re.findall(r'<th scope="row" class="ux-daygrid-time">(\d\d):00–\d\d:00</th>', full_day)
        assert hours and hours[0] == "09" and hours[-1] == "10", f"11d: the day draws empty hours {hours}"
        # 11e. the month legend: counts as words, a request never shown as a booking
        legend = _text(_section(appt("month"), "month-legend"))
        assert " n " not in f" {legend} " and "booked" in legend and "not a booking" in legend, f"11e: {legend}"
        # 11f. one request queue, the same in every view, with zero and with several requests
        queues = [_section(appt(v), "requests") for v in ("day", "week", "month")]
        assert queues[0] == queues[1] == queues[2], "11f: the request queue differs between views"
        assert "Preferred day" in _text(queues[0]) and "not booked" in _text(queues[0]).lower(), "11f: a request reads like a slot"
        dino = conn.execute("SELECT patient_id FROM patients WHERE patient_name = 'Dino Quattro'").fetchone()[0]
        extra = appointments.request(conn, dino, "2031-03-08", "afternoon", "dolore")
        conn.commit()
        q2 = _section(appt("week"), "requests")
        names = re.findall(r'<strong class="ux-request-name">([^<]+)</strong>', q2)
        assert names == ["Dino Quattro", "Carlo Tre"], f"11f: not ordered by preferred day: {names}"
        assert '<span class="ux-count" aria-label="2 requests waiting">2</span>' in q2, "11f: the count does not match"
        for r in conn.execute("SELECT id FROM appointments WHERE status = 'requested'").fetchall():
            conn.execute("UPDATE appointments SET status = 'declined' WHERE id = ?", (r[0],))
        conn.commit()
        q0 = _section(appt("month"), "requests")
        assert "No patient requests waiting" in _text(q0) and "ux-request-name" not in q0, "11f: the empty queue"
        conn.execute("UPDATE appointments SET status = 'requested' WHERE id = ?", (extra,))
        conn.execute("UPDATE appointments SET status = 'requested' WHERE note = 'controllo' AND patient_id = ?", (carlo,))
        conn.commit()
        # 11g. a booking in Week links to the same booking in Day
        wk = _section(appt("week"), "week")
        ids = re.findall(r'href="/appointments\?view=day&amp;day=2031-03-05[^"#]*#appt-(\d+)"', wk)
        dg = _section(appt("day"), "day-grid")
        assert ids and all(f'id="appt-{i}"' in dg for i in ids), f"11g: week links {ids} do not reach the day"
        # 11h. home: All appointments opens this week; next booked day opens that day
        home_a = A.get("/").get_data(as_text=True)
        allv = re.search(r'<a class="ux-link" href="([^"]+)">All appointments', home_a).group(1)
        assert "view=week" in allv and f"day={DAY}" in allv, f"11h: All appointments -> {allv}"
        # 11i. activity: one item per uploaded file, documents named as documents, older work labelled as older
        now_utc = "2031-03-05T08:00:00+00:00"
        rows = [("queue_upload", "drop/n1.txt", now_utc), ("upload_file", "sorted/ZZUA800101010101/notes/n1.txt", now_utc),
                ("sync_note", "sorted/ZZUA800101010101/notes/n1.txt", now_utc),
                ("upload_file", "sorted/ZZUB800101010102/documents/consent.pdf", "2031-01-10T08:00:00+00:00")]
        for action, target, ts in rows:
            conn.execute("INSERT INTO audit_log (ts, username, role, action, target, allowed) VALUES (?, 'assistant', 'assistant', ?, ?, 1)",
                         (ts, action, target))
        conn.commit()
        act = _section(A.get("/").get_data(as_text=True), "activity")
        items = re.findall(r'<li class="ux-activity', act)
        assert len(items) == 2, f"11i: {len(items)} items for two files"
        assert "Note filed" in _text(act) and "Document filed" in _text(act), f"11i: {_text(act)}"
        assert re.search(r"Older.*Document filed", _text(act)) and not re.search(r"Older.*Note filed", _text(act)), \
            f"11i: older activity not labelled: {_text(act)}"
        # 11j. stock is alerted once, still actionable; no separate alerts card repeating the line
        import inventory
        inventory.create_item(conn, "guanti", "pz", 5, "dentist", "dentist")     # balance 0 <= 5: one open alert
        home_s = A.get("/").get_data(as_text=True)
        assert "Review and alerts" not in home_s and 'data-ux="alerts"' not in home_s, "11j: the alerts card repeats the line"
        att = _section(home_s, "attention")
        assert "1 item low on stock" in _text(att) and 'href="/stock"' in att, "11j: the stock alert lost its link"
        assert _text(home_s).count("low on stock") == 1, "11j: the stock alert is repeated"
        # 11k. the operational chart: confirmed bookings per day, next 14 days, with a table equivalent
        chart = _section(A.get("/").get_data(as_text=True), "bookings-14d")
        t = _text(chart)
        assert "next 14 days" in t and "confirmed" in t.lower() and "appointment book" in t, f"11k: {t[:200]}"
        cells = re.findall(r'<tr><th scope="row">([^<]+)</th><td>(\d+)</td></tr>', chart)
        assert len(cells) == 14 and cells[0] == ("Wed 5 Mar", "2") and ("Fri 7 Mar", "1") in cells, f"11k: {cells[:3]}"
        clinic_time.now = lambda env=None: datetime(2031, 2, 10, 9, 0)
        try:
            quiet = _text(_section(A.get("/").get_data(as_text=True), "bookings-14d"))
            clinic_time.now = lambda env=None: datetime(2031, 6, 1, 9, 0)
            none_ahead = _text(_section(A.get("/").get_data(as_text=True), "bookings-14d"))
        finally:
            clinic_time.now = lambda env=None: TODAY
        assert "No confirmed bookings" in quiet and "Wed 5 Mar" in quiet, f"11k: quiet chart: {quiet}"
        assert "No confirmed bookings" in none_ahead and "Next confirmed" not in none_ahead, f"11k: {none_ahead}"
        # 11l. quick actions keep their labels on one line
        assert re.search(r"\.ux-quick-item \{[^}]*white-space: nowrap", css), "11l: quick actions wrap"

        # 12. P29 staff redesign (the patient portal's direction, the staff palette kept)
        css12 = (Path(__file__).parent / "app" / "static" / "css" / "app.css").read_text()
        # 12a. sign out is always reachable: the account foot stays pinned while a long menu scrolls
        foot12 = re.findall(r"\.app-side-foot\s*\{([^}]*)\}", css12)
        assert any("position: sticky" in r and "bottom: 0" in r for r in foot12), "12a: sign out scrolls out of reach"
        # 12b. a fixed-height box scrolls inside and keeps its header row in place
        box12 = re.search(r"\.sx-box\s*\{([^}]*)\}", css12)
        assert box12 and "overflow: auto" in box12.group(1) and "max-height" in box12.group(1) \
            and "position: relative" in box12.group(1), "12b: no fixed-height scroll box"
        assert re.search(r"\.sx-box[^{]*th\s*\{[^}]*position: sticky", css12), "12b: the header row scrolls away"
        assert re.search(r"tbody tr:nth-child\(even(?: of :not\(\[hidden\]\))?\)[^{]*\{[^}]*background", css12), "12b: rows are not told apart"

        # 12c. home opens with what comes next for this person: the dentist's own next patient
        home12 = clients["dentist"].get("/").get_data(as_text=True)
        hero12 = _section(home12, "hero")
        assert 'data-kind="dentist"' in home12 and "Anna Uno" in hero12 and "09:30" in hero12, \
            "12c: the dentist's next patient does not lead home"
        assert re.search(r'<a class="btn[^"]*" href="/patients/ZZUA800101010101"', hero12), "12c: no button to the record"
        agenda12 = _section(home12, "agenda")
        assert re.search(r'<a class="btn[^"]*" href="/patients/ZZUA800101010101"[^>]*>\s*Open', agenda12), \
            "12c: an agenda row's Open is not a button"
        assert 'class="sx-box' in agenda12, "12c: the agenda is not in a fixed-height box"
        # 12d. reception's home opens with the front desk: requests and call-backs, each a button to where it is done
        desk12 = clients["assistant"].get("/").get_data(as_text=True)
        hero12 = _section(desk12, "hero")
        waiting12 = conn.execute("SELECT COUNT(*) FROM appointments WHERE status = 'requested'").fetchone()[0]
        assert 'data-kind="desk"' in desk12 and \
            f"{waiting12} request{'' if waiting12 == 1 else 's'} to confirm" in _text(hero12), "12d: the front desk does not lead"
        assert re.search(r'<a class="btn[^"]*" href="/appointments[^"]*#requests"', hero12) and \
            re.search(r'<a class="btn[^"]*" href="/handoffs"', hero12), "12d: the desk card's actions are not buttons"
        assert "low on stock" not in _text(hero12), "12d: the stock alert is repeated in the desk card"

        # 12e. the patient record is grouped into tabs; every section is still on the page (no script shows them all),
        # and a role only gets the tabs it may open
        for role12, expect12 in (("dentist", ["overview", "clinical", "chart", "care", "files", "billing", "portal", "imaging"]),
                                 ("assistant", ["overview", "care", "files", "billing", "portal", "imaging"])):
            rec12 = clients[role12].get("/patients/ZZUA800101010101").get_data(as_text=True)
            tabs12 = re.findall(r'<button type="button" role="tab" id="tabbtn-(\w+)" aria-controls="tab-(\w+)"', rec12)
            assert [a for a, _ in tabs12] == expect12 and all(a == b for a, b in tabs12), f"12e: {role12} tabs {tabs12}"
            for name in expect12:
                assert f'id="tab-{name}" role="tabpanel" aria-labelledby="tabbtn-{name}"' in rec12, f"12e: no {name} panel"
            for heading in ("Demographics", "Record timeline", "Consent", "Data requests", "Invoices", "Visit dates"):
                assert f">{heading}" in rec12, f"12e: {role12} lost the {heading} section"
            assert 'id="imaging"' in rec12 and 'id="editModal"' in rec12, "12e: a link target or the edit dialog is gone"
            assert ("Clinical history" in rec12) == (role12 == "dentist"), "12e: clinical history shown to the wrong role"
            # NEW-07: the dental chart is the dentist's alone
            assert ("data-dental-chart" in rec12) == (role12 == "dentist"), "12e: dental chart shown to the wrong role"

        # 12f. lists: a button opens each row, long lists keep their height and scroll inside, state filters are tabs
        stock12 = clients["dentist"].get("/stock").get_data(as_text=True)
        item12 = conn.execute("SELECT id FROM inventory_items WHERE name = 'guanti'").fetchone()[0]
        assert len(re.findall(rf'<a class="btn[^"]*" href="/stock/{item12}"', stock12)) >= 2, \
            "12f: the low alert and the item row do not open the item with a button"
        assert len(re.findall(r'class="[^"]*\bsx-box\b', stock12)) >= 2, "12f: the stock lists are not in fixed-height boxes"
        for path12 in ("/handoffs", "/reminders"):
            page12 = clients["assistant"].get(path12).get_data(as_text=True)
            assert re.search(r'<nav class="sx-tabs[^"]*"[^>]*aria-label="Show"', page12) and 'aria-current="page"' in page12, \
                f"12f: {path12} states are not a tab control"
        assert 'style="' not in _section(clients["dentist"].get("/").get_data(as_text=True), "agenda"), \
            "12f: an inline style on home (app_selftest 21 forbids them)"
        # 12g. no list row is opened by clicking its title text: the title is plain, a button opens the row,
        # and an expanding section opens with a button too
        linked12 = [t.name for t in (Path(__file__).parent / "app" / "templates").glob("*.html")
                    if '<a class="ds-row-title' in t.read_text()]
        assert not linked12, f"12g: a row title is still the only link in {sorted(linked12)}"
        summary12 = " ".join(body for sel, body in re.findall(r"([^{}]*)\{([^}]*)\}", css12)
                             if ".ux-upload summary" in [s.strip() for s in sel.split(",")])
        assert "border:" in summary12 and "border-radius" in summary12, \
            "12g: an expanding section opens with a line of link text"

        # 13. P30 top bar (the assistant-panel mockup): a Search button, the clinic clock, what is waiting, the account.
        # every number is one the sidebar and Home already count, for a role that may open the page it counts
        for role13 in ("dentist", "assistant"):
            bar13 = _section(clients[role13].get("/").get_data(as_text=True), "topbar")
            form13 = re.search(r'<form[^>]*role="search"[^>]*action="/patients"[^>]*method="get"|'
                               r'<form[^>]*method="get"[^>]*action="/patients"[^>]*role="search"', bar13)
            assert form13 and re.search(r'<input[^>]*name="q"', bar13) and re.search(r'<button[^>]*type="submit"[^>]*>[^<]*(?:<[^>]+>[^<]*)*Search', bar13), \
                f"13a: {role13}'s top bar search has no Search button"
            assert re.search(r'data-clock[^>]*data-minutes="540"', bar13) and ">09:00<" in bar13 and "Wed 5 Mar" in bar13 \
                and "Europe/Rome" not in bar13 and "clinic time" not in bar13, \
                f"13b: {role13}'s top bar does not show the clinic time"
            assert 'class="dropdown-menu' in bar13 and 'href="/change-password"' in bar13 and 'action="/logout"' in bar13 \
                and 'name="csrf_token"' in bar13 and f">{role13}<" in bar13, \
                f"13d: {role13}'s top bar has no account menu"
        waiting13 = conn.execute("SELECT COUNT(*) FROM appointments WHERE status = 'requested'").fetchone()[0]
        reviews13 = conn.execute("SELECT COUNT(*) FROM note_reviews WHERE status IN ('pending', 'extraction_failed', 'confirming')").fetchone()[0]
        dbar13 = _section(clients["dentist"].get("/").get_data(as_text=True), "topbar")
        abar13 = _section(clients["assistant"].get("/").get_data(as_text=True), "topbar")
        assert waiting13 and f"{waiting13} pending request" in dbar13 and 'href="/appointments#requests"' in dbar13, \
            "13c: the bell does not list the waiting requests"
        assert ("notes to review" in dbar13 or "note to review" in dbar13) == bool(reviews13), "13c: the review count is not the sidebar's"
        assert "to review" not in re.sub(r"imported file[^<]*to review", "", abar13), "13c: the assistant is told about notes only a dentist can open"
        for text13 in ("Mark all as read", "localStorage", "sessionStorage"):
            assert text13 not in dbar13, f"13c: {text13} - nothing is kept in the browser or on the server"
        items13 = len(re.findall(r'class="app-notif-item', dbar13))
        assert items13 and re.search(rf'class="app-bell-count"[^>]*>{items13}<', dbar13), "13c: the badge is not the number of things waiting"
        adm13 = _section(clients["admin"].get("/admin/users").get_data(as_text=True), "topbar")
        assert 'class="app-notif-item' not in adm13 and "Nothing needs you right now" in adm13 and 'role="search"' not in adm13, \
            "13c: admin has nothing waiting and no record search"

        # 14. P30 lists: every list can be searched, filtered and sorted. one script reads what each list declares; the page
        # declares only choices its own rows can answer (a sort key every row carries, a filter value some row holds)
        import handoff
        pid14 = patient_id.resolve(conn, "ZZUA800101010101")
        handoff.raise_request(conn, pid14, "patient_asked", "billing")
        pages14 = {"/patients": ("dentist", {"name", "next", "last"}), "/billing": ("dentist", {"amount", "name"}),
                   "/stock": ("dentist", {"name", "status"}), "/handoffs": ("assistant", {"raised", "name"}),
                   "/appointments": ("assistant", {"day", "asked", "name"})}
        for path14, (role14, keys14) in pages14.items():
            assert_list_tools(clients[role14].get(path14).get_data(as_text=True), path14, keys14)
        assert 'js/list_tools.js' in dentist.get("/patients").get_data(as_text=True), "14b: the lists script is not loaded"
        tools_js14 = (Path(__file__).parent / "app" / "static" / "js" / "list_tools.js").read_text()
        assert "bootstrap" not in tools_js14.lower() or "data-bs-toggle" in tools_js14, "14b: panels open with the page's own dropdown"
        assert not re.search(r"localStorage|sessionStorage|indexedDB|innerHTML\s*=\s*[^;]*\b(?:value|textContent|dataset)", tools_js14), \
            "14b: nothing is stored in the browser and no row text is written back as markup"
        assert "of :not([hidden])" in css12, "14c: shaded rows are counted among the rows that show"
        assert "Search" in re.sub(r"<[^>]+>", " ", dentist.get("/patients").get_data(as_text=True).split('data-ux="patient-rows"')[0]), \
            "14d: the patients box has a Search button"

        # 15. P30 equal boxes: every box on the record has one of three fixed heights and boxes that share a row match; the long
        # tables (patients, invoices) scroll inside a box instead of growing the page
        for role15 in ("dentist", "assistant"):
            rec15 = clients[role15].get("/patients/ZZUA800101010101").get_data(as_text=True)
            for tab15 in re.finditer(r'<div class="sx-panel[^"]*" id="tab-(\w+)" role="tabpanel"(.*?)(?=<div class="sx-panel[^"]*" id="tab-\w+" role="tabpanel"|<dialog|<div class="modal|\Z)', rec15, re.S):
                cards15 = re.findall(r'<section class="card dash-card ([^"]*)" aria-labelledby="([^"]+)"', tab15.group(2))
                assert cards15, f"15a: {role15} {tab15.group(1)} tab has no boxes"
                for classes15, ident15 in cards15:
                    assert re.search(r"\bsx-h-[sml]\b", classes15), f"15a: {role15}: the {ident15} box has no fixed height"
            order15 = re.findall(r'<section class="card dash-card ([^"]*)" aria-labelledby="(access|rights|consent)-title"', rec15)
            if role15 == "dentist":
                assert [i for _c, i in order15] == ["access", "rights", "consent"], f"15a: portal boxes are in the order {order15}"
                assert "sx-wide" in order15[2][0] and order15[0][0].split()[0] == order15[1][0].split()[0], \
                    "15a: the two small portal boxes are not the same size, or consent is not the full-width box below them"
                assert re.search(r'dash-card (sx-h-m sx-wide)" aria-labelledby="timeline-title"', rec15), "15a: the timeline is not full width"
        # 15c. P29 U-003: a visit whose note writes another day says so on the record; a note that agrees says nothing
        import sqlite3 as _sq
        own15 = _sq.connect(str(db))
        dino15 = patient_id.resolve(own15, "ZZUD800101010104")
        own15.execute("INSERT INTO visits (patient_id, visit_date, procedures, clinical_notes, source_path) VALUES"
                      " (?, '2023-08-27', '[]', 'controllo 27 lug, tutto ok', 'sorted/ZZUD800101010104/notes/d.json')", (dino15,))
        own15.commit()
        own15.close()
        dino_rec = dentist.get("/patients/ZZUD800101010104").get_data(as_text=True)
        assert "data-date-conflict" in dino_rec and "27 lug" in dino_rec, "15c U-003: the note's other date is not flagged"
        assert "data-date-conflict" not in rec15, "15c U-003: a note without another date is flagged"
        css15 = css12
        assert re.search(r"\.sx-panel \.dash-card\s*\{[^}]*height: var\(--sx-h", css15) and re.search(r"\.sx-panel-col\s*\{\s*display: contents", css15), \
            "15b: record boxes are not a grid of fixed-height boxes"
        assert re.search(r"\.sx-panel \.dash-card > \.dash-card-body\s*\{[^}]*overflow: auto", css15), "15b: a box's body does not scroll inside"
        assert all(re.search(rf"--sx-h-{k}: \d+rem", css15) for k in "sml"), "15b: the three box heights are not defined"
        for path15, role15 in (("/patients", "dentist"), ("/billing", "dentist")):
            page15 = clients[role15].get(path15).get_data(as_text=True)
            tables15 = re.findall(r'(<div class="sx-box sx-box-table">\s*<table)', page15)
            assert len(tables15) == len(re.findall(r"<table", page15)) and tables15, f"15c: a {path15} table is not in a scroll box"
            assert "table-responsive" not in page15, f"15c: {path15} wraps its table the old way"
        assert re.search(r"\.sx-box-table\s*\{\s*--h: \d+rem", css15), "15c: the table box has no height"

        # 16. P30 record head and tiles: the record opens on a dark band with who this is and when they come next, and the
        # Overview, Billing and Imaging tabs open on a strip of equal tiles whose numbers are the record's own
        anna16 = patient_id.resolve(conn, "ZZUA800101010101")
        billed16 = conn.execute("SELECT COALESCE(SUM(amount), 0), COUNT(*) FROM invoices WHERE patient_id = ?", (anna16,)).fetchone()
        last16 = conn.execute("SELECT MAX(visit_date) FROM visits WHERE patient_id = ?", (anna16,)).fetchone()[0]
        import appointments as appts16
        next16 = appts16.next_booked_local(conn, anna16)
        for role16 in ("dentist", "assistant"):
            rec16 = clients[role16].get("/patients/ZZUA800101010101").get_data(as_text=True)
            head16 = _section(rec16, "record-head")
            assert 'class="sx-rec-head' in rec16 and "Anna Uno</h1>" in head16 and "ZZUA800101010101" in head16, f"16a: {role16} record head"
            assert (next16 or "No booking") in _text(head16), f"16a: {role16} record head does not say when Anna comes next ({next16})"
            assert "/notes/new" in head16, f"16a: {role16} lost Add note"
            for tab16, labels16 in (("overview", ("Next appointment", "Last visit", "Billed")),
                                    ("billing", ("Billed in total", "Billed lines", "Visits on record"))):
                panel16 = rec16.split(f'id="tab-{tab16}"')[1].split('role="tabpanel"', 1)[1]
                tiles16 = re.search(r'<div class="ux-kpis ux-kpis-3 sx-wide"[^>]*>(.*?)<!--/tiles-->', panel16, re.S)
                assert tiles16, f"16b: {role16} {tab16} tab has no tile strip"
                text16 = _text(tiles16.group(1))
                assert all(l in text16 for l in labels16) and tiles16.group(1).count('class="ux-kpi ') == 3, f"16b: {role16} {tab16} tiles: {text16}"
            ov16 = _text(rec16.split('id="tab-overview"')[1].split("<!--/tiles-->")[0])
            assert f"{billed16[0]:.2f}" in ov16 and (last16 or "No visit yet") in ov16, f"16b: the overview tiles are not the record's numbers: {ov16}"
            if 'id="tab-imaging"' in rec16:
                img16 = rec16.split('id="tab-imaging"')[1].split('aria-labelledby="imaging-title"')[0]
                assert '<div class="ux-kpis ux-kpis-3 sx-wide"' in img16, f"16b: {role16} imaging tab has no tile strip"

        # 17. P30 record details: consent is a table whose wording opens under its row with a Read out button (all wording
        # shows without script); reception's Files tab pairs Upload with what happens next; the billed lines are a table
        anna17 = patient_id.resolve(conn, "ZZUA800101010101")
        lines17 = conn.execute("SELECT COUNT(*) FROM invoices WHERE patient_id = ?", (anna17,)).fetchone()[0]
        for role17 in ("dentist", "assistant"):
            rec17 = clients[role17].get("/patients/ZZUA800101010101").get_data(as_text=True)
            box17 = rec17.split('aria-labelledby="consent-title"')[1].split("</section>")[0]
            table17 = re.search(r"<table[^>]*>(.*?)</table>", box17, re.S)
            assert table17 and all(h in _text(table17.group(1)) for h in ("Consent", "Status", "Last change", "Wording")), \
                f"17a: {role17}: consent is not a table"
            rows17 = re.findall(r'<tr data-consent="(\w+)"', box17)
            assert sorted(rows17) == ["ai_assistant", "marketing", "messaging", "recording"], f"17a: consent rows {rows17}"
            for purpose17 in rows17:
                assert re.search(rf'<button[^>]*data-read-out[^>]*aria-controls="wording-{purpose17}"[^>]*hidden', box17) or \
                    re.search(rf'<button[^>]*aria-controls="wording-{purpose17}"[^>]*data-read-out[^>]*hidden', box17), \
                    f"17a: no Read out button for {purpose17} (rendered hidden, shown by the script)"
                wording17 = re.search(rf'<tr id="wording-{purpose17}"[^>]*>(.*?)</tr>', box17, re.S)
                assert wording17 and "Wording to read out" in wording17.group(1) and " hidden" not in wording17.group(0).split(">")[0], \
                    f"17a: the {purpose17} wording is not on the page without script"
            assert re.search(r'class="ds-badge [^"]*"[^>]*>(Not asked|Given|Withdrawn|Ask again)<', box17), "17a: a consent's status is not a badge"
            files17 = rec17.split('id="tab-files"')[1].split('id="tab-')[0]
            boxes17 = re.findall(r'<section class="card dash-card ([^"]*)" aria-labelledby="([^"]+)"', files17)
            if role17 == "assistant":
                assert [b for _c, b in boxes17] == ["upload-title", "next-steps-title"] and boxes17[0][0] == boxes17[1][0] \
                    and "sx-wide" not in boxes17[0][0], f"17b: reception's Files tab is not Upload beside what happens next: {boxes17}"
                assert "Show to patient" in _text(files17), "17b: the steps do not say the patient sees nothing until a dentist shares it"
            else:
                assert "next-steps-title" not in files17, "17b: the dentist does not need reception's steps"
            bill17 = rec17.split('aria-labelledby="invoices-title"')[1].split("</section>")[0]
            assert re.search(r'<table class="ux-table[^"]*"', bill17) and "Description" in bill17 and "Amount" in bill17, \
                f"17c: {role17}: billed lines are not a table"
            assert bill17.count("<tr data-line") == lines17, f"17c: {role17}: {bill17.count('<tr data-line')} rows for {lines17} lines"

        # 18. P30 page tiles: Patients, Appointments, Stock and Call-backs open on three equal tiles, each a number the
        # database holds - never one the page invents
        import inventory as inv18
        want18 = {
            "/patients": ("dentist", {"On record": conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0],
                                      "No phone on file": conn.execute("SELECT COUNT(*) FROM patients WHERE phone IS NULL OR phone = ''").fetchone()[0]}),
            f"/appointments?view=day&day={DAY}": ("assistant", {"Requests to confirm": conn.execute("SELECT COUNT(*) FROM appointments WHERE status = 'requested'").fetchone()[0]}),
            "/stock": ("dentist", {"Items": conn.execute("SELECT COUNT(*) FROM inventory_items").fetchone()[0],
                                   "Open alerts": len(inv18.open_alerts(conn))}),
            "/handoffs": ("assistant", {{"open": "Waiting", "claimed": "Taken"}[s18]: n18 for s18, n18 in
                                        {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM handoff_requests GROUP BY status")}.items()
                                        if s18 in ("open", "claimed")}),
        }
        for path18, (role18, figures18) in want18.items():
            page18 = clients[role18].get(path18).get_data(as_text=True)
            tiles18 = re.search(r'<div class="ux-kpis ux-kpis-3" data-ux="page-tiles">(.*?)<!--/page-tiles-->', page18, re.S)
            assert tiles18 and tiles18.group(1).count('class="ux-kpi ') == 3, f"18: {path18} has no strip of three tiles"
            for label18, n18 in figures18.items():
                tile18 = re.search(rf'<span class="ux-kpi-label">{re.escape(label18)}</span>\s*<span class="ux-kpi-value">([^<]+)</span>', tiles18.group(1))
                assert tile18 and tile18.group(1).strip() == str(n18), f"18: {path18} {label18} shows {tile18 and tile18.group(1)}, the database has {n18}"

        # 19. P30 equal boxes on the pages: Home pairs agenda with requests and activity with quick actions, the chart runs
        # full width; each request opens its preferred day; Appointments stacks the calendar over the requests; Stock pairs
        # its alerts with Add an item over the full-width item list
        def cards19(html, pattern):
            return re.findall(r'<section class="card ux-card ([^"]*)"[^>]*aria-labelledby="(' + pattern + r')-title"', html)
        home19 = clients["assistant"].get("/").get_data(as_text=True)
        got19 = dict((t, c) for c, t in cards19(home19, "agenda|requests|chart|activity|quick"))
        assert set(got19) == {"agenda", "requests", "chart", "activity", "quick"}, f"19a: home boxes {got19}"
        size19 = lambda c: re.search(r"\bsx-h-[sml]\b", c).group(0) if re.search(r"\bsx-h-[sml]\b", c) else None
        assert size19(got19["agenda"]) and size19(got19["agenda"]) == size19(got19["requests"]), "19a: agenda and requests are not one size"
        assert size19(got19["activity"]) and size19(got19["activity"]) == size19(got19["quick"]), "19a: activity and quick actions are not one size"
        assert "sx-wide" in got19["chart"], "19a: the chart is not full width"
        waiting19 = conn.execute("SELECT substr(starts_at, 1, 10) FROM appointments WHERE status = 'requested' ORDER BY starts_at").fetchall()
        req19 = home19.split('aria-labelledby="requests-title"')[1].split("</section>")[0]
        for (day19,) in waiting19[:3]:
            assert re.search(rf'<a class="btn[^"]*" href="/appointments\?view=day&amp;day={day19}#requests"', req19), \
                f"19a: the request for {day19} has no button to its day"
        assert re.search(r"\.ux-home-main, \.ux-home-side \{ display: contents; \}\s*\n", css12.split("P30 equal boxes on the pages")[1]), \
            "19a: home is still two free-height columns"
        appt19 = clients["assistant"].get(f"/appointments?view=day&day={DAY}").get_data(as_text=True)
        assert 'class="ux-appt-layout ux-appt-stacked"' in appt19 and re.search(r'<ul class="ux-list ux-requests sx-box[^"]*" id="request-rows"', appt19), \
            "19b: appointments does not stack the calendar over a requests box"
        stock19 = clients["dentist"].get("/stock").get_data(as_text=True)
        boxes19 = dict((t, c) for c, t in re.findall(r'<section class="card dash-card ([^"]*)" aria-labelledby="(alerts|items|new)-title"', stock19))
        assert 'class="sx-panel sx-panel-2 sx-page"' in stock19 and "sx-wide" in boxes19.get("items", ""), f"19c: stock boxes {boxes19}"
        if "alerts" in boxes19 and "new" in boxes19:
            assert size19(boxes19["alerts"]) and size19(boxes19["alerts"]) == size19(boxes19["new"]), "19c: alerts and Add an item are not one size"

        # 20. P30 billing is one box: All / Outstanding / To reconcile / Drafts choose what shows inside it (every section
        # shows without script, and each keeps its own marker), and the box has a fixed height whose body scrolls
        bill20 = clients["dentist"].get("/billing").get_data(as_text=True)
        box20 = re.search(r'<section class="card ux-card sx-bill sx-h-l" aria-labelledby="bill-title">(.*?)<!--/bill-box--></section>', bill20, re.S)
        assert box20, "20: the invoices are not one fixed-height box"
        for key20 in ("owed", "unknown", "draft"):
            assert f'data-ux="billing-{key20}"' in box20.group(1), f"20: the {key20} section is not inside the box"
            panel20 = re.search(rf'<div class="sx-bill-panel"[^>]*data-bill-panel="{key20}"[^>]*>', box20.group(1))
            assert panel20 and " hidden" not in panel20.group(0), f"20: the {key20} section is hidden without script"
        group20 = re.search(r'<div class="sx-tabs sx-tabs-sm"[^>]*data-bill-show[^>]*>(.*?)</div>', box20.group(1), re.S)
        assert group20 and " hidden" in group20.group(0).split(">")[0], "20: the show control is not added by the script"
        labels20 = re.findall(r'<button type="button" data-show="(\w+)" aria-pressed="(true|false)">([^<]+)', group20.group(1))
        assert [(k, l.strip()) for k, _p, l in labels20] == [("all", "All"), ("owed", "Outstanding"), ("unknown", "To reconcile"), ("draft", "Drafts")] \
            and labels20[0][1] == "true", f"20: show buttons {labels20}"
        assert '<div class="sx-card-body">' in box20.group(1), "20: the box's body does not scroll inside"

        # 21. P30 the remaining lists - notes to review and data requests - get the same tools (the import queue's are
        # checked in patient_files_selftest, which stages a real import)
        import data_rights
        import note_review
        data_rights.file_request(conn, patient_id.resolve(conn, "ZZUA800101010101"), "access", "dentist", "dentist", "synthetic")
        note_review.record_failed(conn, Path(tmp) / "unreadable.txt", "needs_review", "assistant", "assistant")
        assert_list_tools(dentist.get("/reviews").get_data(as_text=True), "/reviews", {"created", "name"})
        assert_list_tools(dentist.get("/data-requests").get_data(as_text=True), "/data-requests", {"filed", "name"})

        # 22. P29 U-021: WebKit - the macOS app's engine - leaves links out of Tab unless asked. the menu and the lookup
        # results ask (tabindex), the results move with the arrow keys, and the app asks for every link
        home22 = dentist.get("/").get_data(as_text=True)
        side22 = home22[home22.index('id="app-side"'):home22.index("</aside>")]
        links22 = re.findall(r"<(?:a|button)\b[^>]*>", side22)
        assert links22 and all('tabindex="0"' in a for a in links22), \
            f"22 U-021: a menu link or button WebKit's Tab skips: {[a for a in links22 if 'tabindex' not in a][:2]}"
        top22 = home22[home22.index('data-ux="topbar"'):]
        for cls in ("app-topbar-toggle", "app-bell", "app-account"):
            tag = re.search(rf'<button[^>]*class="[^"]*{cls}[^"]*"[^>]*>', top22)
            assert tag and 'tabindex="0"' in tag.group(0), f"22 U-021: WebKit's Tab skips the {cls} button"
        found22 = dentist.get("/patients/search?q=Anna").get_data(as_text=True)
        results22 = re.findall(r"<a\b[^>]*>", found22)
        assert results22 and all('tabindex="0"' in a for a in results22), "22 U-021: a lookup result WebKit's Tab skips"
        assert "Enter to open" in found22 and "Esc to close" in found22, "22 U-021: the results do not say the keys"
        js22 = (Path(__file__).parent / "app" / "static" / "js" / "app.js").read_text()
        assert "P29 U-021" in js22, "22 U-021: no keyboard code for the results"
        u021 = js22[js22.index("P29 U-021"):]
        assert "ArrowDown" in u021 and "ArrowUp" in u021 and "Escape" in u021, "22 U-021: no arrow keys on the results"
        swift22 = (Path(__file__).parent / "desktop" / "macos" / "Sources" / "WorkspaceWindow.swift").read_text()
        assert "config.preferences.tabFocusesLinks = true" in swift22, "22 U-021: the app leaves links out of Tab"

        # 23. P29 R-014 / R-015: dates, times and phones in the patients list are kept whole (the browser check in
        # e2e_p29_staff.py measures it at 390, 768 and 1024), and phones get the short-card layout
        plist23 = dentist.get("/patients").get_data(as_text=True)
        assert "ux-table-patients" in plist23, "23 R-014: the patients table has no card layout for phones"
        assert re.search(r'<span class="ux-nowrap">\d{4}-\d{2}-\d{2}', plist23), "23 R-015: a date in the list can break"

        # 24. P29 U-007: every reason box on a staff page grows with what is typed (and so gets the counter); none is a
        # single line that hides what was written
        tdir = Path(__file__).parent / "app" / "templates"
        single = [f"{t.name}: {m}" for t in sorted(tdir.glob("*.html"))
                  for m in re.findall(r'<input[^>]*name="reason"[^>]*>', t.read_text())]
        assert not single, f"24 U-007: single-line reason fields: {single[:3]}"

        # 25. P29 U-008: what an action did comes up as a popup with OK over the updated page. it does not block the
        # rest of the page (owner, 2026-11-03), keeps each message's kind, and comes before the menu for the keyboard
        with dentist.session_transaction() as s25:
            s25["_flashes"] = [("success", "Saved zz25"), ("danger", "Not saved zz25")]
        home25 = dentist.get("/").get_data(as_text=True)
        box25 = re.search(r'<section class="app-result"[^>]*>(.*?)</section>', home25, re.S)
        assert box25, "25 U-008: the result is not a popup"
        tag25 = box25.group(0).split(">")[0]
        assert 'data-ux="result"' in tag25 and 'role="dialog"' in tag25 and 'aria-modal="false"' in tag25, \
            f"25 U-008: the popup is not a dialog that leaves the page usable: {tag25}"
        assert 'data-flash="done"' in box25.group(1) and 'data-flash="not done"' in box25.group(1) \
            and box25.group(1).count("zz25") == 2, "25: the popup lost a message or its kind"
        assert home25.count("zz25") == 2, "25: a message shows twice, in the popup and in a banner"
        ok25 = re.search(r'<button[^>]*data-result-ok[^>]*>\s*OK\s*</button>', box25.group(1))
        assert ok25 and 'tabindex="0"' in ok25.group(0), "25: no OK, or WebKit's Tab skips it"
        assert home25.index('data-ux="result"') < home25.index('id="app-side"'), "25: the OK comes after the whole menu"
        assert 'data-ux="result"' not in dentist.get("/").get_data(as_text=True), "25: the popup comes back by itself"
        js25 = (Path(__file__).parent / "app" / "static" / "js" / "app.js").read_text()
        assert "P29 U-008" in js25, "25 U-008: nothing closes the popup"
        u008 = js25[js25.index("P29 U-008"):].split("\n// P29")[0]
        assert "data-result-ok" in u008 and "Escape" in u008 and "showModal" not in u008, \
            "25: OK or Esc does not close it, or it locks the page"
        css25 = (Path(__file__).parent / "app" / "static" / "css" / "app.css").read_text()
        rule25 = re.search(r"\.app-result \{([^}]*)\}", css25)
        assert rule25 and "position: fixed" in rule25.group(1), "25: the popup sits in the page flow like a banner"

        # 26. P29 NEW-01 / U-005: the menu scrolls in its drawer too (below 992 px it was fixed with nothing to scroll,
        # so the middle items were out of reach), its scroll line is always drawn (macOS hides overlay scrollbars), and
        # it opens scrolled to the page you are on (the browser check in e2e_p29_staff.py measures all three)
        drawer26 = re.search(r"@media \(max-width: 991\.98px\) \{\s*\.app-side \{([^}]*)\}", css25)
        assert drawer26 and "overflow-y: auto" in drawer26.group(1), "26 U-005: the menu drawer cannot be scrolled"
        bar26 = re.search(r"\.app-side::-webkit-scrollbar \{([^}]*)\}", css25)
        assert bar26 and "width" in bar26.group(1) and ".app-side::-webkit-scrollbar-thumb" in css25, \
            "26 NEW-01: the menu's scroll line is left to the system, which hides it on a Mac"
        assert "P29 U-005" in js25 and "show.bs.offcanvas" in js25[js25.index("P29 U-005"):], \
            "26 U-005: the menu does not open at the page you are on"

        # 27. P29 U-010: one Requests page keeps the three kinds apart - patients' appointment requests, imaging work,
        # data requests - each with its count; every row says its status, whose it is, its dates and the next step, and
        # links straight to where that is done; a role sees only the kinds it may act on
        req27 = dentist.get("/requests").get_data(as_text=True)
        kinds27 = re.findall(r'<section[^>]*data-kind="(\w+)"', req27)
        assert kinds27 == ["appointment", "imaging", "data", "services"], f"27 U-010: the kinds are not apart: {kinds27}"

        def part27(kind):
            return re.search(r'data-kind="' + kind + r'".*?</section>', req27, re.S).group(0)

        def first27(kind, who):
            rows = re.findall(r'<li class="ds-row"[^>]*>(.*?)</li>', part27(kind), re.S)
            texts = [" ".join(re.sub(r"<[^>]+>", " ", r).split()) for r in rows]
            return next((t for t in texts if who in t), "")
        for word in ("Carlo Tre", "Waiting to be booked", "Reception or a dentist", "Asked", "Next: book a time or decline it"):
            assert word in first27("appointment", "Carlo Tre"), f"27: an appointment request does not say {word!r}"
        assert 'href="/appointments?view=day&amp;day=2031-03-10' in part27("appointment"), "27: no link to the day it asks for"
        for word in ("Access", "Anna Uno", "A dentist", "Filed", "Next: review it"):
            assert word in first27("data", "Anna Uno"), f"27: a data request does not say {word!r}"
        assert re.search(r'href="/data-requests#req-\d+"', part27("data")), "27: no link to the data request"
        assert "No imaging appointment needs reception action." in part27("imaging"), "27: the empty imaging part"
        n27 = conn.execute("SELECT COUNT(*) FROM appointments WHERE status = 'requested'").fetchone()[0]
        assert re.search(r'id="kind-appointment-title"[^>]*>Appointment requests <span class="ux-count">' + str(n27) + '</span>', req27), \
            "27: a kind has no count, or the wrong one"
        kinds27r = re.findall(r'<section[^>]*data-kind="(\w+)"', clients["assistant"].get("/requests").get_data(as_text=True))
        assert kinds27r == ["appointment", "imaging", "services"], f"27: reception sees {kinds27r}"
        assert clients["admin"].get("/requests").status_code == 302, "27: admin has no requests"

        # 9. the demo seed's tags carry no codice fiscale
        import demo_seed
        demo_seed.seed(conn, seed=22, anchor="2031-03-10")
        tags = [r[0] for r in conn.execute("SELECT note FROM appointments WHERE note LIKE 'Demo %'")]
        assert tags and not any(CF_SHAPE.search(t) for t in tags), f"9: seed tags {tags[:2]}"
        assert not conn.execute("SELECT 1 FROM appointments WHERE note LIKE 'demo-seed:%'").fetchone(), "9: an old tag remains"
        conn.close()
    print("selftest ok")


def _empties(html):
    return [" ".join(re.sub(r"<[^>]+>", " ", m).split())
            for m in re.findall(r'<p class="(?:dash-empty-line|ux-empty)[^"]*">(.*?)</p>', html, re.S)]


def empty_states():
    """P29 U-006: on an empty clinic every list says what puts something there or what to do next, not only that it
    is empty. one patient with nothing on file, one stock item with no movements."""
    import inventory
    import app.db as app_db
    import clinic_guides
    from app import create_app
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "empty.sqlite"
        conn = storage.init_db(str(db))
        for user, role in (("dentist", "dentist"), ("assistant", "assistant")):
            conn.execute("INSERT INTO users (username, password_hash, role, active) VALUES (?, ?, ?, 1)",
                         (user, generate_password_hash(f"{user}-pw-123"), role))
        cf = "ZZUE800101010101"
        patient_id.seed_patient(conn, cf, "Elsa Vuota", "+393401110000")
        conn.commit()
        app_db.DB_PATH = str(db)
        clinic_guides.DB_PATH = str(Path(tmp) / "guides.sqlite")
        clinic_guides.STORE = Path(tmp) / "guides"
        app = create_app()
        app.config["WTF_CSRF_ENABLED"] = False
        clients = {u: _client(app, u, f"{u}-pw-123") for u in ("dentist", "assistant")}
        want = [
            ("dentist", "/handoffs", ["Nobody is waiting.", "asks the portal's assistant for a person"]),
            ("dentist", "/handoffs?status=claimed", ["Nothing in that state.", "Show all call-backs"]),
            ("dentist", "/reminders", ["Nothing is queued.", "booked appointment"]),
            ("dentist", "/data-requests", ["No data requests.", "from the patient's record"]),
            ("dentist", "/stock", ["No items yet.", "Add one in Add an item"]),
            ("dentist", "/stock?show=low", ["No items yet."]),
            ("assistant", "/stock", ["No items yet.", "A dentist adds them"]),
            ("dentist", "/reviews", ["Nothing is waiting.", "An uploaded note waits here"]),
            ("dentist", f"/patients/{cf}/documents", ["Nothing waiting.", "until a dentist confirms or rejects it"]),
            ("dentist", f"/patients/{cf}/billing", ["No invoices on record.", "when a visit with prices is filed",
                                                    "No payments recorded.", "on an issued invoice"]),
            ("dentist", f"/patients/{cf}", ["No visits on record.", "Filing a note for this patient adds one",
                                            "No data requests.", "File one below"]),
            ("assistant", f"/patients/{cf}", ["No data requests.", "File one below"]),
        ]
        for role, path, phrases in want:
            page = clients[role].get(path).get_data(as_text=True)
            said = " | ".join(_empties(page))
            for phrase in phrases:
                assert phrase in said, f"U-006 {role} {path}: the empty line does not say {phrase!r}: {said!r}"
        item = inventory.create_item(conn, "Vuoto zz", "pz", 5, "dentist", "dentist")
        conn.commit()
        said = " | ".join(_empties(clients["dentist"].get(f"/stock/{item}").get_data(as_text=True)))
        assert "No movements yet." in said and "Record a delivery, a use or a count above" in said, f"U-006 stock item: {said!r}"
        low = " | ".join(_empties(clients["dentist"].get("/stock?show=low").get_data(as_text=True)))
        assert low == "", f"U-006: a low list with a low item still says it is empty: {low!r}"
        inventory.move(conn, item, "receipt", 40, "dentist", "dentist", key="u6-1")
        conn.commit()
        low = " | ".join(_empties(clients["dentist"].get("/stock?show=low").get_data(as_text=True)))
        assert "Nothing is low" in low and "above its alert level" in low and "Show all 1" in low, f"U-006 low: {low!r}"
        conn.close()
    print("empty states ok")


def paged_patients():
    """P29 U-004: the patients list comes in pages of 100 with row numbers; filter, sort and search act on every
    patient on the server (10,000 rows took 15-24 s to show in a browser), and every link keeps them."""
    import app.db as app_db
    import clinic_guides
    from app import create_app
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "paged.sqlite"
        conn = storage.init_db(str(db))
        conn.execute("INSERT INTO users (username, password_hash, role, active) VALUES ('dentist', ?, 'dentist', 1)",
                     (generate_password_hash("dentist-pw-123"),))
        for i in range(230):
            patient_id.seed_patient(conn, f"ZZPG{i:04d}0101010A", f"Paziente {chr(65 + i % 26)}{i:03d}",
                                    None if i % 4 == 0 else f"+39340{i:07d}")
        conn.commit()
        app_db.DB_PATH = str(db)
        clinic_guides.DB_PATH = str(Path(tmp) / "guides.sqlite")
        clinic_guides.STORE = Path(tmp) / "guides"
        app = create_app()
        app.config["WTF_CSRF_ENABLED"] = False
        dentist = _client(app, "dentist", "dentist-pw-123")

        def view(url):
            page = dentist.get(url).get_data(as_text=True)
            rows = re.findall(r'<tr data-patient="[^"]+" data-name="([^"]+)"', page)
            numbers = [int(n) for n in re.findall(r'<td data-label="#" class="ux-rownum">(\d+)</td>', page)]
            tools = re.search(r"<div class=\"sx-tools\"[^>]*data-list-tools[^>]*>", page)
            return page, rows, numbers, tools.group(0) if tools else ""
        page, rows, numbers, tools = view("/patients")
        assert len(rows) == 100 and numbers == list(range(1, 101)), f"U-004: page one has {len(rows)} rows, numbers {numbers[:3]}"
        assert "data-server" in tools and 'data-shown="230"' in tools and 'data-total="230"' in tools, f"U-004 tools {tools}"
        assert "Page 1 of 3" in _text(page) and re.search(r'href="/patients\?[^"]*page=2[^"]*"[^>]*>Next', page), "U-004: no pager"
        page, rows, numbers, _t = view("/patients?page=3")
        assert len(rows) == 30 and numbers == list(range(201, 231)), f"U-004: the last page {len(rows)} {numbers[:2]}"
        assert "Showing 201-230 of 230" in _text(page), "U-004: the pager does not say which rows these are"
        assert view("/patients?page=99")[2][0] == 201, "U-004: a page past the end is not the last page"
        page, rows, numbers, tools = view("/patients?phone=no")
        nophone = conn.execute("SELECT COUNT(*) FROM patients WHERE phone IS NULL OR phone = ''").fetchone()[0]
        assert len(rows) == nophone == 58 and 'data-shown="58"' in tools and "\"phone\": [\"no\"]" in tools, \
            f"U-004: a filter does not act on every patient: {len(rows)} {tools}"
        heads = re.findall(r'<th scope="col"[^>]*><a class="sx-th" href="([^"]+)"', page)
        assert heads and all("phone=no" in h for h in heads), f"U-004: a column header drops the filter: {heads}"
        page, rows, _n, _t = view("/patients?phone=no&sort=name&dir=desc")
        assert rows == sorted(rows, reverse=True), "U-004: the server sort is not applied to the filtered rows"
        page, rows, _n, tools = view("/patients?q=paziente%20a")
        assert rows and all(r.startswith("Paziente A") for r in rows), "U-004: search across pages"
        js = (Path(__file__).parent / "app" / "static" / "js" / "list_tools.js").read_text()
        assert "data-server" in js, "U-004: the list tools have no server mode"
        conn.close()
    print("paged patients ok")


def main():
    if "--selftest" not in sys.argv:
        print("usage: python ux_selftest.py --selftest")
        return
    real = clinic_time.now
    clinic_time.now = lambda env=None: TODAY
    try:
        selftest()
        empty_states()
        paged_patients()
    finally:
        clinic_time.now = real


if __name__ == "__main__":
    main()
