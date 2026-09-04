"""Gates for the public site app.

This is the one app with no authentication gate, so its selftest is about
what it *cannot* reach rather than what it refuses. The no-CDN rule is here
from the app's first commit on purpose: applied to only two of three apps it
would be a rule that quietly stopped being true.
"""

import ast
import inspect
import re
import sys

from markupsafe import escape

from site_app import content, create_site_app


def on_page(value, page_text):
    """Is this config string rendered on the page?

    Compared in its escaped form: jinja autoescapes, so a value containing an
    apostrophe reaches the html as &#39; and a raw comparison silently fails
    on exactly the strings most likely to be real prose.
    """
    return str(escape(value)) in page_text


def selftest():
    app = create_site_app()
    client = app.test_client()

    # 1. structural isolation. a route that cannot reach patient data cannot
    # leak it, which is a stronger property than a gate a later edit could
    # lose. same shape as patient_app_selftest section 1.
    tree = ast.parse(inspect.getsource(sys.modules["site_app"]))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for forbidden in ("storage", "sqlite3", "patient_auth", "patient_accessor",
                      "patient_app", "web_auth", "web_session", "auth", "app", "agent", "ask"):
        assert forbidden not in imported, \
            f"1: the public site must not import {forbidden} - it has no business reaching data"

    # 2. and it holds no database path or connection of any kind
    src = inspect.getsource(sys.modules["site_app"])
    assert "clinic.sqlite" not in src, "1: the public site must not name the database"
    assert "SECRET_KEY" not in app.config or app.config["SECRET_KEY"] is None, \
        "2: the public site writes no session, so it should hold no secret key"

    # 3. cookies are not port-scoped and all three apps share a host (CR-05),
    # so if session state ever arrives here it must not land on "session"
    assert app.config["SESSION_COOKIE_NAME"] == "site_session", \
        "3: the site app needs its own cookie name, distinct from staff and patient"
    assert app.config["SESSION_COOKIE_NAME"] != "staff_csrf", \
        "3: must not collide with the staff app's cookie"

    # 4. the reference page renders, unauthenticated, with no session
    page = client.get("/reference")
    assert page.status_code == 200, "4: the reference page should render with no session"
    assert b"Design system reference" in page.data, "4: the reference page should carry its heading"
    # and it sets no cookie at all - not merely a differently-named one
    assert "Set-Cookie" not in page.headers, \
        "4: the public site should set no cookie; it has no session to keep"

    # 5. no CDN. the same rule the other two apps carry, from commit one.
    assert not re.search(
        r'<(?:script|link)[^>]+(?:src|href)="https?://', page.text, re.IGNORECASE
    ), "5: the public site must not reference any external http(s) asset"

    # 6. no inline styles. phase 30 got the staff surface to zero and this app
    # starts there rather than being cleaned up later.
    assert not re.search(r"<[^>]+\sstyle=", page.text), \
        "6: no inline style= - a page needing one means the token set is incomplete"

    # 7. the shared layer resolves end to end. a <link> to a 404 looks
    # identical to a working one in markup, so request both files and look
    # for something only the real file carries.
    tokens = client.get("/shared/css/tokens.css")
    assert tokens.status_code == 200, "7: the shared token stylesheet must serve"
    assert b"--ds-primary" in tokens.data, "7: tokens.css must carry the design tokens"
    components = client.get("/shared/css/components.css")
    assert components.status_code == 200, "7: the shared component stylesheet must serve"
    assert b".ds-btn" in components.data, "7: components.css must carry the component layer"
    assert client.get("/shared/fonts/InterVariable.woff2").status_code == 200, \
        "7: the vendored typeface must serve locally"

    # 8. the reference page is a reference: every component family in
    # components.css has to appear on it. an unrendered component is an
    # unreviewed one, and this is what stops the page rotting as the layer
    # grows.
    families = [
        "ds-btn-primary", "ds-btn-secondary", "ds-btn-ghost", "ds-btn-danger", "ds-btn-success",
        "ds-input", "ds-textarea", "ds-select", "ds-check", "ds-help",
        "ds-field-error", "ds-error-text", "ds-label",
        "ds-card", "ds-card-header", "ds-card-body", "ds-card-footer",
        "ds-table", "ds-table-cards", "ds-table-wrap",
        "ds-badge", "ds-alert", "ds-tabs", "ds-tab",
        "ds-modal", "ds-modal-header", "ds-modal-body", "ds-modal-footer",
        "ds-avatar", "ds-breadcrumb", "ds-status",
        "ds-state", "ds-state-icon", "ds-state-title", "ds-state-body",
        "ds-state-error", "ds-spinner", "ds-sr-only",
        "ds-exchange", "ds-turn", "ds-turn-user", "ds-turn-agent",
    ]
    missing = [f for f in families if f not in page.text]
    assert not missing, f"8: the reference page must render every component family, missing: {missing}"

    # 9. no bootstrap-icon markup. this app deliberately loads no icon font,
    # so a <i class="bi"> here renders nothing - and phases 33-38 copy their
    # markup off this page, which is how dead markup spreads.
    assert 'class="bi' not in page.text, \
        "9: the site app loads no icon font - bi- markup here renders nothing and will be copied"

    # 10. UX-07 - clinic content comes from config, never a template.
    # asserting a string appears is not enough: a hardcoded phone number
    # would pass that too. The test that bites is that CHANGING the config
    # changes the render. Rendered through the app's own jinja env because
    # the chrome is what carries this content, and /reference deliberately
    # drops the chrome.
    cfg = content.load()
    header_tpl = app.jinja_env.get_template("_header.html")
    footer_tpl = app.jinja_env.get_template("_footer.html")
    header_html = header_tpl.render(clinic=cfg)
    footer_html = footer_tpl.render(clinic=cfg)

    assert cfg["clinic"]["name"] in header_html, "10: the header must render the clinic name from config"
    assert cfg["actions"]["book_label"] in header_html, "10: the primary action label comes from config"
    assert cfg["contact"]["phone"] in footer_html, "10: the footer phone number comes from config"
    for item in cfg["nav"]:
        assert item["label"] in header_html, f"10: nav item {item['label']!r} missing from the header"

    # the mutation, run as part of the suite rather than by hand: a different
    # config must produce a different page. if the name were hardcoded this
    # fails.
    import copy
    mutated = copy.deepcopy(cfg)
    mutated["clinic"]["name"] = "ZZ Config Probe"
    mutated["contact"]["phone"] = "00 0000 0000"
    assert "ZZ Config Probe" in header_tpl.render(clinic=mutated), \
        "10: changing the config must change the header - the name is hardcoded"
    assert "00 0000 0000" in footer_tpl.render(clinic=mutated), \
        "10: changing the config must change the footer - the phone is hardcoded"

    # 11. the loader refuses a file missing a required section. a site that
    # boots with its doctors silently absent is worse than one that will not
    # boot at all.
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write("clinic:\n  name: x\n")
        broken = fh.name
    try:
        content.load(broken)
        raise AssertionError("11: a config missing required sections must not load")
    except RuntimeError as exc:
        assert "missing required section" in str(exc), \
            f"11: expected a missing-section error, got: {exc}"

    # 12. the icon sprite is present and inline. section 9 forbids icon-font
    # markup; this asserts the replacement actually shipped, so "no icons at
    # all" cannot pass both.
    assert '<symbol id="i-tooth"' in page.text, \
        "12: the inline svg sprite must be included in the base template"
    assert "@font-face" not in page.text, "12: no icon font may be declared inline"

    # 13. the header's menu toggle is a real button with the accessible
    # state on it. a div with a click handler passes every visual check and
    # no keyboard user can open it.
    assert 'aria-expanded="false"' in header_html, \
        "13: the menu toggle must carry aria-expanded"
    assert "<button" in header_html and "site-menu-toggle" in header_html, \
        "13: the menu toggle must be a button, not a div with a handler"

    # 14. UX-06 - the landing page renders and its content traces to config.
    home = client.get("/")
    assert home.status_code == 200, "14: / must render the landing page"
    assert on_page(cfg["hero"]["headline"], home.text), "14: the hero headline comes from config"
    assert on_page(cfg["hero"]["eyebrow"], home.text), "14: the hero eyebrow comes from config"
    for svc in cfg["services"]["entries"]:
        assert on_page(svc["title"], home.text), f"14: service {svc['title']!r} missing from the page"
    for stat in cfg["stats"]["entries"]:
        assert on_page(stat["value"], home.text), f"14: stat {stat['value']!r} missing from the page"
    for item in cfg["why_us"]["entries"]:
        assert on_page(item["title"], home.text), f"14: why-us item {item['title']!r} missing"

    # 15. no config key may collide with a dict method name. jinja resolves
    # foo.items to dict.items, not to the key - which rendered a 500 on the
    # first build of this page. cheap to assert once, invisible until a
    # section is added months later.
    DICT_METHODS = {"items", "keys", "values", "get", "pop", "copy", "update", "clear", "setdefault"}
    def walk_keys(node, path="clinic"):
        if isinstance(node, dict):
            for k, v in node.items():
                assert k not in DICT_METHODS, \
                    f"15: config key {path}.{k} collides with a dict method - jinja will resolve the method"
                walk_keys(v, f"{path}.{k}")
        elif isinstance(node, list):
            for v in node:
                walk_keys(v, path)
    walk_keys(cfg)

    # 16. D-06 - nothing on the public site claims to book, pay or confirm.
    # there is no appointments table and no payment path in this product, so
    # a form here could only be a fake success.
    assert "<form" not in home.text, \
        "16: the public site has nothing to post to - a form here would be a fake success path"
    assert on_page(cfg["booking"]["note"], home.text), \
        "16: the booking section must state that nothing is confirmed on this page"

    # 17. D-05 - the assistant preview is a preview, and cannot pretend.
    assert on_page(cfg["assistant"]["preview"]["label"], home.text), \
        "17: the preview must carry its label - an unlabelled transcript reads as live"
    assert on_page(cfg["assistant"]["preview"]["voice_note"], home.text), \
        "17: the preview must say voice is unavailable rather than drawing a live-looking mic"
    assert re.search(r"<button[^>]+disabled", home.text), \
        "17: the voice control must render disabled, not as a button that looks live"
    assert "<textarea" not in home.text, \
        "17: no chat input on the public site - there is no endpoint behind it"
    assert on_page(cfg["assistant"]["verification_note"], home.text), \
        "17: the section must state that patient data requires verification"

    # 18. every person, quote and answer traces to config (UX-07 again, for
    # the sections most tempting to hardcode)
    for doc in cfg["doctors"]["entries"]:
        assert on_page(doc["name"], home.text) and on_page(doc["role"], home.text), \
            f"18: doctor {doc['name']!r} missing from the page"
    for person in cfg["staff"]["entries"]:
        assert on_page(person["name"], home.text), f"18: staff {person['name']!r} missing"
    for item in cfg["faq"]["entries"]:
        assert on_page(item["q"], home.text) and on_page(item["a"], home.text), \
            f"18: faq entry {item['q']!r} missing from the page"
    for step in cfg["journey"]["steps"]:
        assert on_page(step["title"], home.text), f"18: journey step {step['title']!r} missing"
    assert on_page(cfg["contact"]["phone"], home.text), "18: the contact phone comes from config"
    for h in cfg["hours"]:
        assert on_page(h["day"], home.text), f"18: opening hours row {h['day']!r} missing"

    # 19. link integrity. a site is mostly links, and a dead one is invisible
    # to every other check here.
    #
    # emptied by 33-04, which built the four pages this list was holding.
    # it stays as an empty set on purpose: a future plan that needs to defer
    # a link has somewhere honest to record it, and anything not recorded
    # here fails.
    PENDING_ROUTES = set()
    routes = {r.rule for r in app.url_map.iter_rules()}
    hrefs = set(re.findall(r'href="([^"]+)"', home.text))
    anchors = set(re.findall(r'id="([^"]+)"', home.text))
    broken_paths, broken_anchors = set(), set()
    for href in hrefs:
        if href.startswith("http") or href.startswith("tel:") or href.startswith("mailto:"):
            continue
        path, _, frag = href.partition("#")
        if path in ("", "/"):
            if frag and frag not in anchors:
                broken_anchors.add(href)
        elif path.startswith(("/static/", "/shared/")):
            # parameterised rules - an exact rule match says nothing, so
            # fetch it. this proves the asset actually serves, which is what
            # a reader of the page needs anyway.
            assert client.get(path).status_code == 200, \
                f"19: asset {path} is linked but does not serve"
        elif path not in routes:
            broken_paths.add(path)
    assert not broken_anchors, f"19: in-page anchors with no target: {sorted(broken_anchors)}"
    unexpected = broken_paths - PENDING_ROUTES
    assert not unexpected, f"19: links to routes that do not exist: {sorted(unexpected)}"

    # 20. UX-09 - the secondary pages render, and every one of them is held
    # to the same rules as the landing page. checking only "/" would have let
    # four pages ship ungated.
    PUBLIC_PAGES = ["/", "/services", "/doctors", "/clinic", "/contact", "/assistant"]
    for path in PUBLIC_PAGES:
        r = client.get(path)
        assert r.status_code == 200, f"20: {path} must render"
        assert not re.search(
            r'<(?:script|link)[^>]+(?:src|href)="https?://', r.text, re.IGNORECASE
        ), f"20: {path} must not reference an external http(s) asset"
        assert not re.search(r"<[^>]+\sstyle=", r.text), f"20: {path} has an inline style="
        assert 'class="bi' not in r.text, f"20: {path} carries icon-font markup"
        # PROPERTY NARROWED, not dropped: a form is a fake success path only
        # where nothing handles it. /assistant now has a real handler that
        # answers from clinic.yaml and touches no data, so it may carry one -
        # and it may post ONLY to itself. Every other page still has nothing
        # to post to and must still have no form.
        if path == "/assistant":
            forms = re.findall(r'<form[^>]*action="([^"]*)"', r.text)
            assert forms == ["/assistant"], \
                f"20: /assistant must post only to itself, found {forms}"
        else:
            assert "<form" not in r.text, f"20: {path} has a form - there is nothing to post to"
        # exactly one h1 per page. two is an outline error a screen reader
        # reads aloud, and it is how a page head and a section heading
        # duplicate each other unnoticed - which /contact did.
        h1s = re.findall(r"<h1[^>]*>(.*?)</h1>", r.text, re.S)
        assert len(h1s) == 1, f"20: {path} has {len(h1s)} h1 elements, expected exactly 1"
        # no two headings may carry the same text. this is how a page head
        # and a section heading duplicate each other - /contact rendered
        # "Contact" as both an h1 and an h2, and the h1 count above did not
        # see it because they were different levels.
        # scoped to <main>: the footer's column headings are navigation
        # labels, and "Services" there legitimately matches the /services
        # page title.
        main_html = re.search(r"<main[^>]*>(.*)</main>", r.text, re.S)
        assert main_html, f"20: {path} has no <main> landmark"
        heads = [h.strip().lower() for h in
                 re.findall(r"<h[1-3][^>]*>(.*?)</h[1-3]>", main_html.group(1), re.S)]
        dupes = {h for h in heads if heads.count(h) > 1}
        assert not dupes, f"20: {path} repeats heading text at more than one level: {sorted(dupes)}"
        page_anchors = set(re.findall(r'id="([^"]+)"', r.text))
        for href in set(re.findall(r'href="([^"]+)"', r.text)):
            if href.startswith(("http", "tel:", "mailto:")):
                continue
            hpath, _, frag = href.partition("#")
            if hpath in ("", "/") and not hpath:
                assert not frag or frag in page_anchors, \
                    f"20: {path} has a dead in-page anchor {href}"
            elif hpath and not hpath.startswith(("/static/", "/shared/")):
                assert hpath in routes, f"20: {path} links to {hpath}, which is not a route"

    # 20e. the "AI assistant" nav must lead to the assistant page, NOT to a
    # login form. it pointed straight at the patient app's /login, so the
    # nav dumped visitors on a bare form with no explanation - the defect
    # this page replaces.
    assert cfg["actions"]["assistant_href"] == "/assistant", \
        "20e: the assistant action must point at the assistant page, not a login url"
    # every nav/footer link labelled for the assistant resolves to the page,
    # never to a login url
    for item in cfg["nav"] + [l for c in cfg["footer"]["columns"] for l in c["links"]]:
        if "assistant" in item["label"].lower():
            assert item["href"] == "/assistant", \
                f"20e: {item['label']!r} points at {item['href']!r}, not the assistant page"
    a_page = client.get("/assistant")
    assert a_page.status_code == 200, "20e: the assistant page must render"
    # it is public and holds nothing: the real chat is behind the patient
    # session, and this page links there rather than standing in for it
    assert "Set-Cookie" not in a_page.headers, "20e: the assistant page keeps no session"
    assert cfg["actions"]["assistant_signin_href"] in a_page.text, \
        "20e: and must offer the way in to the authenticated chat"

    # 20f. it says plainly what the assistant cannot do. speech exists now,
    # but only behind the VOICE_DEMO fence and off by default - see 20i - and
    # the assistant still cannot read records, book, or remember a turn. a
    # page implying otherwise would promise capabilities that do not exist.
    for item in cfg["assistant"]["cannot"]:
        assert on_page(item, a_page.text), f"20f: missing limitation: {item[:40]!r}"
    assert on_page(cfg["assistant"]["cannot_heading"], a_page.text), \
        "20f: the limitations need their heading"

    # 20g. the public assistant answers clinic questions and NEVER personal
    # ones. it has no database - section 1 asserts that structurally - so a
    # personal question must hand off to sign-in rather than be answered.
    from site_app import clinic_answers
    clinic_answers.selftest()

    for q in ("when is my next appointment?", "how much do i owe?", "quanto devo pagare?"):
        r = client.post("/assistant", data={"question": q})
        assert r.status_code == 200, f"20g: /assistant should answer {q!r}"
        assert "agent-turn--handoff" in r.text, \
            f"20g: {q!r} must hand off to sign-in, not be answered here"
        assert cfg["actions"]["assistant_signin_href"] in r.text, \
            "20g: and must offer the way in"

    ans = client.post("/assistant", data={"question": "what are your opening hours?"})
    assert "agent-turn--answer" in ans.text, "20g: a clinic question should be answered"
    assert on_page(cfg["hours"][0]["day"], ans.text), \
        "20g: and the answer must come from clinic.yaml"

    off = client.post("/assistant", data={"question": "what is the capital of france?"})
    assert "agent-turn--refusal" in off.text, "20g: off-topic must be refused, not guessed"

    # 20h. the json turn endpoint carries the same three outcomes as the
    # form path, and the same refusal to answer anything personal. a second
    # code path would be a second place for that rule to be forgotten - it
    # calls the same router.
    import json as _json
    for q, want in (("what are your opening hours?", "answer"),
                    ("when is my next appointment?", "handoff"),
                    ("who won the football", "refusal")):
        r = client.post("/assistant/ask", data={"question": q})
        assert r.status_code == 200, f"20h: /assistant/ask should answer {q!r}"
        got = _json.loads(r.data)
        assert got["state"] == want, f"20h: {q!r} -> {got['state']}, expected {want}"
        assert got["text"], "20h: every outcome carries text"
    # and it holds no patient data to leak, like every other route here
    r = _json.loads(client.post("/assistant/ask", data={"question": "how much do i owe?"}).data)
    assert r["state"] == "handoff" and "sign in" in r["text"].lower(), \
        "20h: a personal question must hand off, never be answered"

    # 20i. voice obeys the same fence as everything else. with VOICE_DEMO
    # unset - which is the default, and what the suite runs under - the
    # status endpoint says so and the audio endpoints refuse. the page reads
    # that and hides the control rather than offering one that cannot work.
    st = _json.loads(client.get("/assistant/voice/status").data)
    assert st["available"] is False, \
        "20i: voice must be off by default in a suite run"
    assert "VOICE_DEMO" in st["reason"], "20i: and must say why"
    assert client.get("/assistant/voice/greeting").status_code == 503, \
        "20i: the greeting endpoint must refuse when the fence is closed"
    assert client.post("/assistant/voice").status_code == 503, \
        "20i: the turn endpoint must refuse when the fence is closed"

    # 20j. no vendor credential may reach the browser. the page posts audio
    # here and gets text and sound back; the keys are only ever used server
    # side.
    for probe in ("DEEPGRAM", "ELEVENLABS", "api_key", "xi-api-key", "Authorization: Token"):
        assert probe not in a_page.text, f"20j: {probe!r} appears in the page source"
    js = client.get("/static/js/agent.js")
    assert js.status_code == 200, "20j: agent.js should serve"
    for probe in ("DEEPGRAM", "ELEVENLABS", "sk-", "api.deepgram.com", "api.elevenlabs.io"):
        assert probe not in js.text, f"20j: {probe!r} appears in client javascript"

    # 21. route inventory. this app has no authentication gate, so it must
    # not be able to grow a route unnoticed. asserting the exact set is cheap
    # now and impossible to retrofit convincingly later.
    EXPECTED_ENDPOINTS = {
        "static", "shared", "home", "services", "doctors", "clinic_page",
        "contact", "reference", "assistant", "assistant_ask",
        "voice_status", "voice_greeting", "voice_turn",
    }
    actual = {r.endpoint for r in app.url_map.iter_rules()}
    assert actual == EXPECTED_ENDPOINTS, (
        f"21: the public app's route set changed. added={sorted(actual - EXPECTED_ENDPOINTS)} "
        f"removed={sorted(EXPECTED_ENDPOINTS - actual)}. A new public route on an app with no "
        f"auth gate is a deliberate decision - update this set when you make it."
    )

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("run with --selftest")


if __name__ == "__main__":
    main()
