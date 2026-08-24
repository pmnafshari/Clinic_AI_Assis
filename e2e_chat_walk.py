"""the 13-step patient chat walk, driven through a real browser.

runs chromium via playwright against the two live servers, so the browser's own
form serialisation, the required-input constraint and chat.js all actually run.
that is the layer DEF-2 and DEF-3 lived in and the layer run_selftests.sh cannot
reach - patient_app_selftest.py posts form bodies built in python against a
stubbed model, which is exactly why both defects passed it.

the model is NOT stubbed here. answers come from ollama, so this is slow
(~3-4s per question) and it is the only automated instrument that would have
caught the prompt-layout fabrication found on 2026-08-18.

usage:  .venv/bin/python e2e_chat_walk.py [--headed]

needs:  run.py on 5000, PATIENT_COOKIE_SECURE=0 patient_run.py on 5001,
        ollama serving llama3.2:3b.

seeds its own throwaway patients under ZZE* codici fiscali and deletes them in
a finally block, so it never touches fixtures another walk is holding.
"""

import json
import os
import re
import sqlite3
import sys
import urllib.parse
import urllib.request

import patient_auth
from eval_chat import date_variants
from patient_app.strings import t

STAFF_URL = "http://127.0.0.1:5000"
# overridable so the SAME 13 steps run locally and over a tunnel - a second
# copy of this script would drift from this one. staff_url deliberately does
# not move: it is only the pre-flight check, and the staff app is never exposed.
PATIENT_URL = os.environ.get("PATIENT_BASE_URL", "http://127.0.0.1:5001")
DB_PATH = "db/clinic.sqlite"

# codici fiscali must match ^[A-Z]{4}[0-9]{12}$ - ZZE* keeps this script's rows
# out of the UAT* namespace a manual walk uses
ANNA = "ZZEA850010150401"
BRUNO = "ZZEB850010150402"
CARLA = "ZZEC850010150403"
# three visits, three invoice lines - DEF-5's case, which no single-invoice
# patient can exercise
DARIO = "ZZED850010150404"
NEW_PIN = "19283746"

RESULTS = []


def check(step, ok, note):
    RESULTS.append((step, bool(ok), note))
    print(f"  {'PASS' if ok else 'FAIL'}  {step}: {note}")
    return bool(ok)


# --- fixtures -------------------------------------------------------------


def seed():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = [
        (ANNA, "Anna Verdi", "3331110001"),
        (BRUNO, "Bruno Neri", "3472220002"),
        (CARLA, "Carla Bianchi", "3495550003"),
        (DARIO, "Dario Costa", "3386660004"),
    ]
    for cf, name, phone in rows:
        conn.execute(
            "INSERT INTO patients (codice_fiscale, patient_name, phone) VALUES (?, ?, ?)",
            (cf, name, phone),
        )
    visits = [
        (ANNA, "2026-03-04", ["filling 47"], "2026-09-15", "e2e/anna.json",
         [(120.50, "otturazione")]),
        (BRUNO, "2026-05-19", ["rct 46"], "2026-11-02", "e2e/bruno.json",
         [(340.00, "cura canalare")]),
        # carla gets none on purpose - she is step 12's empty-records case
        (DARIO, "2025-11-12", ["filling 36"], "2026-01-20", "e2e/dario-1.json",
         [(90.00, "otturazione")]),
        (DARIO, "2026-01-20", ["rct 24", "ext 38"], "2026-04-08", "e2e/dario-2.json",
         [(340.00, "cura canalare"), (150.00, "estrazione")]),
        (DARIO, "2026-04-08", ["seal 16"], "2026-10-05", "e2e/dario-3.json", []),
    ]
    for cf, date, procs, nxt, path, lines in visits:
        conn.execute(
            "INSERT INTO visits (codice_fiscale, visit_date, procedures, clinical_notes,"
            " next_appointment, source_path) VALUES (?, ?, ?, ?, ?, ?)",
            (cf, date, json.dumps(procs), "", nxt, path),
        )
        visit_id = conn.execute(
            "SELECT id FROM visits WHERE source_path = ?", (path,)
        ).fetchone()["id"]
        for idx, (amount, desc) in enumerate(lines):
            conn.execute(
                "INSERT INTO invoices (codice_fiscale, visit_id, line_index, amount,"
                " description) VALUES (?, ?, ?, ?, ?)",
                (cf, visit_id, idx, amount, desc),
            )
    conn.commit()
    pins = {cf: patient_auth.issue_pin(cf, conn, "dentist", "dentist") for cf, _, _ in rows}
    conn.close()
    return pins


def cleanup():
    conn = sqlite3.connect(DB_PATH)
    cfs = (ANNA, BRUNO, CARLA, DARIO)
    marks = ",".join("?" * len(cfs))
    # child-first, same order the manual walk's task 3 used. every table here
    # is keyed by codice_fiscale - patient_login_attempts deliberately is not
    # on the list, because it is keyed by ip and holds no patient rows to
    # delete. no try/except: a delete that cannot run is a cleanup that did
    # not happen, and it should be loud.
    for table in ("invoices", "visits", "patient_sessions",
                  "patient_credentials", "patients"):
        conn.execute(f"DELETE FROM {table} WHERE codice_fiscale IN ({marks})", cfs)
    conn.commit()
    left = conn.execute(
        f"SELECT COUNT(*) FROM patients WHERE codice_fiscale IN ({marks})", cfs
    ).fetchone()[0]
    conn.close()
    return left


def scope_violations():
    conn = sqlite3.connect(DB_PATH)
    n = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action = 'patient_scope_violation'"
    ).fetchone()[0]
    conn.close()
    return n


# --- browser helpers ------------------------------------------------------


def posted_questions(page):
    """every question value this page has POSTed to /chat, in order.

    read off the raw request body rather than the rendered answer, because
    DEF-3 was a serialisation bug - the page looked fine and the body carried
    two values for one field.
    """
    seen = []

    def on_request(request):
        if request.method == "POST" and request.url.rstrip("/").endswith("/chat"):
            body = request.post_data or ""
            seen.append(body)

    page.on("request", on_request)
    return seen


def question_values(body):
    return urllib.parse.parse_qs(body, keep_blank_values=True).get("question", [])


def submit_form(page, action_fragment):
    # scoped to the form's own submit button on purpose. patient_base.html
    # renders an "Esci" logout button that is also type=submit and sits above
    # the content, so a bare button[type=submit] logs out instead of
    # submitting - phase 17's own fix, and a live trap for a naive selector.
    page.click(f"form[action*='{action_fragment}'] button[type=submit]")
    page.wait_for_load_state("networkidle")


def sign_in(page, cf, pin):
    page.goto(f"{PATIENT_URL}/login")
    page.fill("#codice_fiscale", cf)
    page.fill("#pin", pin)
    submit_form(page, "login")
    if "/change-pin" in page.url:
        # the current-pin field is only rendered when the change is voluntary;
        # a forced change (must_change_pin=1) omits it entirely
        if page.query_selector("#current"):
            page.fill("#current", pin)
        page.fill("#pin", NEW_PIN)
        page.fill("#confirm", NEW_PIN)
        submit_form(page, "change-pin")
    return page.url


def ask(page, text):
    """type a question and submit through the button, returning the answer card text."""
    page.fill("#question", text)
    page.click("#chat-submit")
    page.wait_for_load_state("networkidle")
    return response_text(page)


def response_text(page):
    card = page.query_selector(".chat-response")
    return card.inner_text() if card else ""


def icon_colour(page):
    icon = page.query_selector(".chat-response-icon")
    if icon is None:
        return None
    return icon.evaluate("el => getComputedStyle(el).color")


# --- the walk -------------------------------------------------------------


def walk(browser, pins):
    ctx = browser.new_context()
    page = ctx.new_page()
    page.set_default_timeout(60000)
    bodies = posted_questions(page)

    # 1. login, forced pin change, land on home
    landed = sign_in(page, ANNA, pins[ANNA])
    check("01 login + forced change-pin",
          landed.rstrip("/").endswith(":5001") or landed.endswith("/"),
          f"landed on {landed}")

    # 2. home card and its CTA
    home_ok = t("home_heading", "it") in page.content()
    page.click(f"text={t('home_cta', 'it')}")
    page.wait_for_load_state("networkidle")
    check("02 home card -> /chat",
          home_ok and page.url.endswith("/chat"),
          f"heading present={home_ok}, now at {page.url}")

    # 3. the empty chat page
    content = page.content()
    chips = page.query_selector_all(".chat-example")
    second_sentence = t("chat_intro", "it").split(". ")[-1]
    step3 = (
        t("chat_heading", "it") in content
        and second_sentence in content
        and len(chips) == 4
        and t("question_label", "it") in content
        and t("chat_cta", "it") in content
        and page.query_selector(".chat-response") is None
    )
    check("03 empty chat page",
          step3,
          f"{len(chips)} chips, heading/intro/label/cta present, no answer card")

    # the section-28 markup invariant, asserted on rendered markup rather than
    # on the template source: exactly one element may be named "question"
    named = page.eval_on_selector_all(
        "[name=question]", "els => els.map(e => e.tagName + '#' + e.id)"
    )
    check("03b one element named 'question' (DEF-2/DEF-3 invariant)",
          named == ["INPUT#question"],
          f"elements named question = {named}")

    # 4. DEF-2 - a chip click must actually POST, and show the pending state
    before = len(bodies)
    chip_text = chips[0].inner_text().strip()
    # the pending state is captured from inside the page, at the moment chat.js
    # sets it. reading it from the test after the click is a race the test
    # loses - the POST finishes and the fresh document is already rendered, so
    # you measure the new page's button and always see "Chiedi". this listener
    # is registered after chat.js's, so it runs second and observes what
    # chat.js just did, and sessionStorage survives the navigation.
    page.evaluate("""() => {
        document.getElementById('chat-form').addEventListener('submit', function () {
            var b = document.getElementById('chat-submit');
            sessionStorage.setItem('e2ePending', JSON.stringify({
                disabled: b.disabled,
                label: b.querySelector('.chat-submit-label').textContent,
                spinner: !b.querySelector('.spinner-border').classList.contains('d-none'),
                help: !document.getElementById('chat-pending-help').classList.contains('d-none')
            }));
        });
    }""")
    with page.expect_navigation(wait_until="networkidle"):
        chips[0].click()
    pending = json.loads(page.evaluate("() => sessionStorage.getItem('e2ePending')") or "{}")
    posted = bodies[before:]
    values = question_values(posted[0]) if posted else []
    check("04 DEF-2 chip click reaches the server",
          len(posted) == 1 and values == [chip_text],
          f"POST bodies={len(posted)}, question values={values!r}")
    check("04b pending state during generation",
          pending.get("disabled") and pending.get("label") == t("chat_pending_cta", "it")
          and pending.get("spinner") and pending.get("help"),
          f"{pending}")
    check("04c chip click returns an answer card",
          page.query_selector(".chat-response") is not None,
          f"state class = {page.get_attribute('.chat-response', 'class')}")

    # DEF-3 - enter in the field must submit the typed text, not chip 1
    typed = "Che numero di telefono avete per me?"
    before = len(bodies)
    page.fill("#question", typed)
    with page.expect_navigation(wait_until="networkidle"):
        page.press("#question", "Enter")
    posted = bodies[before:]
    values = question_values(posted[0]) if posted else []
    check("04d DEF-3 Enter submits the typed text",
          len(posted) == 1 and values == [typed] and chip_text not in values,
          f"question values={values!r} (chip 1 was {chip_text!r})")
    check("04e DEF-3 exactly one value serialised for 'question'",
          len(values) == 1,
          f"{len(values)} value(s) in the POST body")

    # 5. Anna's own appointment, faithfully restated
    page.goto(f"{PATIENT_URL}/chat")
    answer = ask(page, t("chat_example_1", "it"))
    accepted = date_variants("2026-09-15", "it")
    hit = [v for v in accepted if v.lower() in answer.lower()]
    check("05 Anna's appointment restated (DEF-1)",
          bool(hit),
          f"matched {hit} in {answer.splitlines()[-1][:70]!r}" if hit else f"none of {accepted} in {answer!r}")
    anna_answers = [answer]

    # 6. Bruno in his own session, plus isolation both ways
    ctx_b = browser.new_context()
    page_b = ctx_b.new_page()
    page_b.set_default_timeout(60000)
    sign_in(page_b, BRUNO, pins[BRUNO])
    page_b.goto(f"{PATIENT_URL}/chat")
    bruno_appt = ask(page_b, t("chat_example_1", "it"))
    bruno_inv = ask(page_b, t("chat_example_3", "it"))
    bruno_answers = [bruno_appt, bruno_inv]

    accepted_b = date_variants("2026-11-02", "it")
    check("06 Bruno's own appointment restated",
          any(v.lower() in bruno_appt.lower() for v in accepted_b),
          bruno_appt.splitlines()[-1][:70] if bruno_appt else "<empty>")
    check("06b Bruno's own invoice restated",
          "340" in bruno_inv,
          bruno_inv.splitlines()[-1][:70] if bruno_inv else "<empty>")

    anna_values = ["Verdi", "3331110001", "04/03/2026", "15/09/2026", "otturazione", "120,50"]
    bruno_values = ["Neri", "3472220002", "19/05/2026", "02/11/2026", "canalare", "340,00"]
    leaked_into_bruno = [v for v in anna_values if any(v in a for a in bruno_answers)]
    leaked_into_anna = [v for v in bruno_values if any(v in a for a in anna_answers)]
    check("06c cross-patient isolation, both directions",
          not leaked_into_bruno and not leaked_into_anna,
          f"anna->bruno {leaked_into_bruno}, bruno->anna {leaked_into_anna}")

    # 7. one response region, and a reload clears it (D-03)
    page.goto(f"{PATIENT_URL}/chat")
    first = ask(page, t("chat_example_1", "it"))
    second = ask(page, t("chat_example_3", "it"))
    cards = page.query_selector_all(".chat-response")
    first_line = first.splitlines()[-1] if first else "<none>"
    check("07 second answer replaces the first",
          len(cards) == 1 and first_line not in page.inner_text("body"),
          f"{len(cards)} response card(s), previous answer absent")
    page.goto(f"{PATIENT_URL}/chat")
    check("07b reload returns the empty state",
          page.query_selector(".chat-response") is None,
          "no answer card after reload")

    # 8. deflection card
    ask(page, "Ho dolore al dente, cosa devo fare?")
    colour = icon_colour(page)
    klass = page.get_attribute(".chat-response", "class") or ""
    check("08 deflection card",
          "chat-response--deflection" in klass
          and page.query_selector(".bi-telephone") is not None
          and colour == "rgb(47, 133, 90)"
          and t("deflect_body", "it") in page.inner_text(".chat-response"),
          f"icon colour {colour}, telephone icon present")

    # 9. refusal card for an off-surface question
    ask(page, "Qual è la capitale della Francia?")
    colour = icon_colour(page)
    klass = page.get_attribute(".chat-response", "class") or ""
    body = page.inner_text(".chat-response")
    topics = ["le tue visite", "il prossimo appuntamento", "le fatture", "i tuoi dati anagrafici"]
    check("09 refusal card names all four topics",
          "chat-response--refusal" in klass
          and page.query_selector(".bi-info-circle") is not None
          and colour == "rgb(108, 117, 125)"
          and all(x in body for x in topics),
          f"icon colour {colour}, {sum(x in body for x in topics)}/4 topics named")

    # 10. english pass
    page.goto(f"{PATIENT_URL}/lang/en")
    page.goto(f"{PATIENT_URL}/chat")
    en = page.content()
    en_ok = (
        t("chat_heading", "en") in en
        and t("chat_example_1", "en") in en
        and t("question_label", "en") in en
        and t("chat_cta", "en") in en
    )
    ask(page, "What is the capital of France?")
    en_refusal = page.inner_text(".chat-response")
    leftover_it = t("refusal_heading", "it") in en_refusal
    check("10 english pass",
          en_ok and t("refusal_heading", "en") in en_refusal and not leftover_it,
          f"english chrome ok={en_ok}, refusal in english, no italian left behind")
    page.goto(f"{PATIENT_URL}/lang/it")

    # 11. 390px - a real viewport, not an iframe. this is the one step the
    # manual walk had to simulate, because chrome would not resize that small.
    ctx_narrow = browser.new_context(viewport={"width": 390, "height": 820})
    page_n = ctx_narrow.new_page()
    page_n.set_default_timeout(60000)
    sign_in(page_n, CARLA, pins[CARLA])
    page_n.goto(f"{PATIENT_URL}/chat")
    metrics = page_n.evaluate("""() => {
        const chips = [...document.querySelectorAll('.chat-example')];
        const tops = new Set(chips.map(c => Math.round(c.getBoundingClientRect().top)));
        const card = document.querySelector('.patient-card').getBoundingClientRect();
        const bar = document.querySelector('[class*=patient-lang]').getBoundingClientRect();
        return {
            rows: tops.size,
            scrollWidth: document.documentElement.scrollWidth,
            clientWidth: document.documentElement.clientWidth,
            cardLeft: Math.round(card.left), cardRight: Math.round(card.right),
            barLeft: Math.round(bar.left), barRight: Math.round(bar.right),
        };
    }""")
    check("11 390px layout",
          metrics["rows"] == 4
          and metrics["scrollWidth"] == metrics["clientWidth"] == 390
          and metrics["cardLeft"] == metrics["barLeft"]
          and metrics["cardRight"] == metrics["barRight"],
          f"chips on {metrics['rows']} rows, no h-overflow "
          f"({metrics['scrollWidth']}=={metrics['clientWidth']}), edges aligned")

    # 12. Carla has no visits - the structural refusal, not a fabrication
    ctx_c = browser.new_context()
    page_c = ctx_c.new_page()
    page_c.set_default_timeout(60000)
    page_c.goto(f"{PATIENT_URL}/login")
    page_c.fill("#codice_fiscale", CARLA)
    page_c.fill("#pin", NEW_PIN)
    submit_form(page_c, "login")
    page_c.goto(f"{PATIENT_URL}/chat")
    carla = ask(page_c, t("chat_example_2", "it"))
    klass = page_c.get_attribute(".chat-response", "class") or ""
    fabricated = re.search(r"\d{2}/\d{2}/\d{4}", carla)
    check("12 empty records -> structural refusal (SC4)",
          "chat-response--refusal" in klass and not fabricated,
          f"refusal class present={('chat-response--refusal' in klass)}, "
          "no invented date in the card")

    # 12b. DEF-5 on the live surface. three invoice lines across three visits:
    # the total must be the python-computed 580,00, and every visit date and
    # tooth number must survive the compound context. eval_chat.py covers this
    # in-process; this is the same case through a browser.
    ctx_d = browser.new_context()
    page_d = ctx_d.new_page()
    page_d.set_default_timeout(60000)
    sign_in(page_d, DARIO, pins[DARIO])
    page_d.goto(f"{PATIENT_URL}/chat")
    total = ask(page_d, t("chat_example_3", "it"))
    check("12b DEF-5 invoice total is the python sum",
          "580" in total,
          total.splitlines()[-1][:70] if total else "<empty>")
    seen = ask(page_d, t("chat_example_2", "it"))
    teeth = [n for n in ("36", "24", "38", "16") if n in seen]
    dates = [d for d in ("12/11/2025", "20/01/2026", "08/04/2026") if d in seen]
    check("12c DEF-5 all three visits survive the compound context",
          len(teeth) == 4 and len(dates) == 3,
          f"{len(dates)}/3 dates, {len(teeth)}/4 tooth numbers")
    ctx_d.close()

    # 13. the narrow card is unchanged; chat uses the wide variant
    widths = {}
    for path in ("/login", "/chat"):
        page_c.goto(f"{PATIENT_URL}{path}")
        widths[path] = page_c.evaluate(
            "() => Math.round(document.querySelector('.patient-card').getBoundingClientRect().width)"
        )
    check("13 login card narrow, chat card wide",
          widths["/login"] <= 420 and widths["/chat"] > widths["/login"],
          f"login {widths['/login']}px vs chat {widths['/chat']}px")

    for c in (ctx, ctx_b, ctx_narrow, ctx_c):
        c.close()


def main():
    from playwright.sync_api import sync_playwright

    for name, url in (("staff", STAFF_URL), ("patient", f"{PATIENT_URL}/login")):
        try:
            urllib.request.urlopen(url, timeout=5)
        except Exception as exc:
            if not isinstance(exc, urllib.error.HTTPError):
                print(f"pre-flight: {name} app not reachable at {url} - {exc}")
                return 2
    try:
        urllib.request.urlopen("http://localhost:11434/api/tags", timeout=5)
    except Exception as exc:
        print(f"pre-flight: ollama not reachable - {exc}")
        return 2

    before = scope_violations()
    pins = seed()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless="--headed" not in sys.argv)
            try:
                walk(browser, pins)
            finally:
                browser.close()
    finally:
        left = cleanup()

    after = scope_violations()
    check("14 no scope violations logged during the walk",
          after == before,
          f"patient_scope_violation {before} -> {after}")
    check("15 fixtures cleaned up",
          left == 0,
          f"{left} throwaway patient row(s) left behind")

    failed = [s for s, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
