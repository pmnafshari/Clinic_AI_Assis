"""Jarvis's own page and status API on 127.0.0.1:5020. Loopback only. The page shows the truth; it never starts a
conversation (Jarvis is hands-free) and closing it changes nothing.

Since J02 follow-up 6 the stream sends a heartbeat every HEARTBEAT_SECONDS, so the page can tell a quiet service from a
lost one: an error on the stream, or STALE_MS without anything, replaces the state with "not reachable" instead of
leaving the last one up. A click on the confirmation is answered with what became of it, and the page says so.

Since the J02 UI review: one overall line says whether the demo is ready and, if not, what is missing and who can fix
it; only that line, the state and its meaning are announced (aria-live), and only when they change - the answer card is
rebuilt only when the answer itself changes, so a screen reader hears each answer once; a cited answer names the device
and links to its page in the clinic app (opened there under the viewer's own sign-in, so role limits stay the clinic
app's); conflicting documents are listed with links and nothing is chosen; the card says the clock time it is discarded
at; and a click keeps the keyboard focus on the page."""
import json

from flask import Flask, Response, abort, jsonify, render_template_string, request

PORT = 5020
CLINIC_URL = "http://127.0.0.1:5000"
HOSTS = {"127.0.0.1", "localhost", f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
LOOPBACK = {"127.0.0.1", "::1"}
HEARTBEAT_SECONDS = 2

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jarvis</title>
<style>
  :root { --bg: #f6f8fc; --ink: #142036; --muted: #4a5568; --card: #fff; --line: #d9e1ec;
          --ok: #1f7a45; --warn: #9a5b00; --bad: #a3261b; --info: #2453d4; }
  body { margin: 0; font: 16px/1.5 -apple-system, "Helvetica Neue", Arial, sans-serif; background: var(--bg); color: var(--ink); }
  main { max-width: 44rem; margin: 0 auto; padding: 24px 16px; }
  .card { background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 20px; margin-bottom: 16px; }
  .state { font-size: 28px; font-weight: 700; letter-spacing: .02em; }
  .READY { color: var(--ok); } .ACTIVE, .CONFIRMING, .WAITING_FOR_CONFIRMATION { color: var(--info); }
  .AUTH_REQUIRED { color: var(--warn); } .DEGRADED, .UNREACHABLE { color: var(--bad); } .STARTING { color: var(--muted); }
  .WAITING_FOR_CONFIRMATION { font-size: 22px; overflow-wrap: anywhere; }
  #confirm-status:empty { display: none; }
  .ready-line { font-weight: 600; margin-top: 0; } .ready-line.ok { color: var(--ok); } .ready-line.no { color: var(--warn); }
  .source { margin: 4px 0 12px; } .source a { color: var(--info); }
  #confirm-status { font-weight: 600; }
  .muted { color: var(--muted); } ol { padding-left: 1.2rem; } code { font-size: 14px; }
  blockquote { margin: 12px 0; padding: 8px 14px; border-left: 4px solid var(--info); background: var(--bg); }
  .warn { color: var(--warn); font-weight: 600; }
  .heard { font-size: 20px; font-weight: 600; }
  .choices { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 12px; }
  .choices button { font: inherit; font-weight: 600; padding: 10px 18px; border-radius: 10px; cursor: pointer;
                    border: 2px solid var(--info); }
  .choices .yes { background: var(--info); color: #fff; } .choices .no { background: var(--card); color: var(--info); }
  .choices button:focus-visible { outline: 3px solid var(--ink); outline-offset: 2px; }
  .choices button:disabled { opacity: .6; cursor: default; }
</style></head>
<body><main data-clinic="{{ clinic_url }}">
  <p class="muted">Jarvis · local voice companion · this page only shows it; Jarvis keeps running when it is closed</p>
  <section class="card">
    <div aria-live="polite" aria-atomic="true">
    <p id="ready" class="ready-line {{ 'ok' if s.ready.ok else 'no' }}">{{ s.ready.text }}</p>
    <p class="muted">Listening:</p>
    <h1 class="state {{ s.state }}" id="state">{{ s.state }}</h1>
    <p id="meaning">{{ s.meaning }}</p>
    </div>
    <p><strong>Why:</strong> <span id="reason">{{ s.reason }}</span></p>
    <p class="muted">Since <span id="since">{{ s.since }}</span> (UTC)</p>
    <p><strong>Clinic-guide answers:</strong> <span id="answers">{{ s.answers.detail }}</span></p>
    <p class="muted">Clinic link (needed for clinic-guide answers, not for listening):
      <span id="clinic">{{ s.clinic.detail }}</span></p>
  </section>
  <p id="confirm-status" class="card" role="status" aria-live="polite"></p>
  {% set a = s.answer %}
  <section class="card" id="answer" aria-live="polite"{% if not a %} hidden{% endif %}>
    <h2 id="answer-title" tabindex="-1">Last question</h2>
    <p class="muted">Answers are shown here on screen, not spoken. They stay for two minutes and are kept nowhere.</p>
    <div id="answer-body">{% if a and a.title %}<h3>{{ a.title }}</h3>{% endif %}{% if a and a.outcome == "confirm" %}
      <h3>Did I hear you right?</h3>
      <p class="heard">{{ a.heard }}</p>
      <p class="muted">Nothing is asked until you press Yes. If you do not, it is discarded in {{ a.left or a.seconds }}
        seconds; nothing is ever sent by itself.</p>
      <div class="choices" data-id="{{ a.id }}"><button type="button" class="yes" data-decision="ask">Yes, ask this</button>
        <button type="button" class="no" data-decision="discard">No, discard</button></div>
    {% elif a %}
      {% if a.heard %}<p><strong>Heard:</strong> {{ a.heard }}</p>{% endif %}
      {% if a.asked_as and a.asked_as != a.heard %}<p><strong>Asked as:</strong> {{ a.asked_as }}</p>{% endif %}
      {% for c in a.citations %}<blockquote>{{ c.passage }}</blockquote>
      <p class="muted">{{ c.title }}{% if c.edition %}, edition {{ c.edition }}{% endif %}, page {{ c.page }} - verified against the page</p>{% endfor %}
      {% for w in a.warnings %}<p class="warn">{{ w.text }} (page {{ w.page }})</p>{% endfor %}
      {% if a.message %}<p>{{ a.message }}</p>{% endif %}
      {% if a.escalation %}<p class="muted">{{ a.escalation }}</p>{% endif %}
      {% if a.devices %}<p>Say which one, for example: {% for d in a.devices %}{{ d.make }} {{ d.model }} ({{ d.room }}){% if not loop.last %}; {% endif %}{% endfor %}</p>{% endif %}
    {% endif %}</div>
  </section>
  <section class="card">
    <h2>How Jarvis works</h2>
    <p>Jarvis starts when you log in to this computer and listens only for its wake phrase. You never click to talk.
    Anything about a patient needs a staff member's own signed-in session, delegated to this device in the clinic app
    (<em>Jarvis</em> in the sidebar). A voice, a name or a spoken "yes" never proves who you are.</p>
    <ol class="muted">
      <li>STARTING - opening the microphone and the wake engine; READY only once real sound is being heard</li>
      <li>READY - only the wake phrase is listened for; nothing is recorded or sent</li>
      <li>ACTIVE - one request after a short tone (at most 15 seconds), then worked out on this computer</li>
      <li>WAITING_FOR_CONFIRMATION - what was heard appears here, and nothing is asked until you press <em>Yes, ask
        this</em>; nothing is recorded meanwhile, and saying the wake phrase again starts over; then the clinic-guide
        answer appears with the document and page it comes from (nothing is spoken back); then back to READY</li>
      <li>AUTH_REQUIRED - needs a delegated staff session; nothing protected is said</li>
      <li>CONFIRMING - a spoken confirmation of an action that is already authorised</li>
      <li>DEGRADED - not available, with the reason (asleep, microphone denied or muted, engine missing, fault)</li>
    </ol>
  </section>
</main>
<script>
  const CLINIC = document.querySelector("main").dataset.clinic;
  const STALE_MS = 4000;           // two heartbeats missed: with the 1 s check below, a frozen service shows within 5 s
  const es = new EventSource("/events");
  let lastSeen = Date.now(), lost = false;
  function seen() {
    lastSeen = Date.now();
    if (!lost) return;
    lost = false;                  // back after a freeze: a heartbeat carries no state, so read the whole state again
    fetch("/status").then((r) => r.json()).then(render).catch(() => {});
  }
  function unreachable(why) {
    if (lost) return;
    lost = true;
    const h = document.getElementById("state");
    h.textContent = "NOT REACHABLE"; h.className = "state UNREACHABLE";
    setReady({ok: false, text: "Not ready: Jarvis is not reachable."});
    document.getElementById("meaning").textContent = "Jarvis is not reachable - the state shown is unknown";
    document.getElementById("reason").textContent = why + " at " + new Date().toLocaleTimeString() +
      "; this page keeps trying";
    document.getElementById("answers").textContent = "unknown while Jarvis is not reachable";
    document.getElementById("clinic").textContent = "unknown while Jarvis is not reachable";
    showAnswer(null);
  }
  es.onmessage = (e) => { seen(); render(JSON.parse(e.data)); };
  // a line is written only when its text changes: the announced lines are then read once per change, not per update
  function setText(id, text) {
    const el = document.getElementById(id);
    if (el.textContent !== text) el.textContent = text;
  }
  function setReady(r) {
    setText("ready", r.text);
    document.getElementById("ready").className = "ready-line " + (r.ok ? "ok" : "no");
  }
  function render(s) {
    setReady(s.ready);
    setText("state", s.state);
    document.getElementById("state").className = "state " + s.state;
    setText("meaning", s.meaning);
    setText("reason", s.reason);
    setText("since", s.since);
    setText("answers", s.answers.detail);
    setText("clinic", s.clinic.detail);
    showAnswer(s.answer);
  }
  es.addEventListener("ping", seen);
  es.onerror = () => unreachable("this page lost its connection to the Jarvis service");
  setInterval(() => { if (Date.now() - lastSeen > STALE_MS) unreachable("no word from the Jarvis service"); }, 1000);
  // text only: what was heard and what the guides say are never treated as markup
  function line(tag, text, cls) {
    const el = document.createElement(tag);
    el.textContent = text;
    if (cls) el.className = cls;
    return el;
  }
  const REFUSED = {
    expired: "Too late: the time to confirm had passed, so nothing was asked. Say the wake phrase and ask again.",
    gone: "This question is no longer waiting (cancelled, or already decided) - nothing was asked.",
    ambiguous: "More than one choice reached Jarvis, so nothing was asked. Say the wake phrase and ask again.",
  };
  function decide(id, decision, box) {
    const status = document.getElementById("confirm-status");
    // the buttons are about to go: the focus moves to the card's heading, where the reply will appear
    if (box.contains(document.activeElement)) document.getElementById("answer-title").focus();
    for (const b of box.querySelectorAll("button")) b.disabled = true;
    status.textContent = "Sending your choice...";
    fetch("/confirm", {method: "POST", headers: {"Content-Type": "application/json"},
                       body: JSON.stringify({id: id, decision: decision})})
      .then((r) => {
        if (r.status === 204) {
          status.textContent = decision === "ask" ? "Confirmed - asking the clinic guides." : "Discarded - nothing was asked.";
          return;
        }
        return r.json().catch(() => ({})).then((body) => {
          status.textContent = REFUSED[body.result] || "Jarvis did not take your choice - nothing was asked.";
        });
      })
      .catch(() => {
        status.textContent = "Your choice did not reach Jarvis - nothing was asked. Check that Jarvis is running.";
        for (const b of box.querySelectorAll("button")) b.disabled = false;
      });
  }
  function choices(id) {
    const box = line("div", "", "choices");
    for (const [label, decision, cls] of [["Yes, ask this", "ask", "yes"], ["No, discard", "discard", "no"]]) {
      const b = line("button", label, cls);
      b.type = "button";
      b.addEventListener("click", () => decide(id, decision, box));
      box.append(b);
    }
    return box;
  }
  for (const box of document.querySelectorAll(".choices")) {
    for (const b of box.querySelectorAll("button")) b.addEventListener("click", () => decide(box.dataset.id, b.dataset.decision, box));
  }
  // a page of the clinic app, opened there under the viewer's own sign-in (the clinic app keeps the role limits)
  function sourceLink(sid, page, text) {
    if (!Number.isInteger(sid) || !Number.isInteger(page)) return line("p", text, "muted");
    const p = line("p", "", "source"), a = line("a", text + " (opens the clinic app in a new tab)");
    a.href = CLINIC + "/guides/sources/" + sid + "?page=" + page + "#page-" + page;
    a.target = "_blank"; a.rel = "noopener noreferrer";
    p.append(a);
    return p;
  }
  let shown = "null";
  function showAnswer(a) {
    const card = document.getElementById("answer"), body = document.getElementById("answer-body");
    card.hidden = !a;
    const now = JSON.stringify(a && a.outcome === "confirm" ? {id: a.id} : a);
    if (now === shown) return;      // the same answer or card: not rebuilt, so not announced again
    shown = now;
    body.replaceChildren();
    if (!a) return;
    if (a.title) body.append(line("h3", a.title));
    if (a.outcome === "confirm") {
      document.getElementById("confirm-status").textContent = "";
      const left = a.left || a.seconds;
      const at = new Date(Date.now() + left * 1000).toLocaleTimeString();
      body.append(line("h3", "Did I hear you right?"), line("p", a.heard, "heard"),
                  line("p", "Nothing is asked until you press Yes. If you do not, it is discarded at " + at + " (in " +
                       left + " seconds); nothing is ever sent by itself.", "muted"), choices(a.id));
      return;
    }
    document.getElementById("confirm-status").textContent = "";   // the card itself now says what became of it
    if (a.heard) body.append(line("p", "Heard: " + a.heard));
    if (a.asked_as && a.asked_as !== a.heard) body.append(line("p", "Asked as: " + a.asked_as));
    const d = a.device;
    for (const c of a.citations || []) {
      const quote = line("blockquote", c.passage);
      if (c.language) quote.lang = c.language;
      body.append(quote);
      body.append(line("p", (d ? d.make + " " + d.model + " (" + d.room + ") - " : "") + c.title + ", " +
                       (c.edition || "version " + c.version) + ", page " + c.page + " - verified against the page", "muted"));
      if (c.from_figure) body.append(line("p", "Read from a picture on the page - check it on the page image.", "muted"));
      body.append(sourceLink(c.source_id, c.page, "Open page " + c.page + " of " + c.title));
    }
    if ((a.warnings || []).length) body.append(line("p", "Warnings in this document:", "warn"));
    for (const w of a.warnings || []) body.append(line("p", w.text + " (page " + w.page + ")", "warn"));
    if (a.message) body.append(line("p", a.message));
    for (const x of a.conflicting || []) body.append(sourceLink(x.source_id, x.page, (x.title || "document") + ", page " + x.page));
    if (a.see && a.reason === "unreadable") body.append(sourceLink(a.see.source_id, a.see.page, "Open page " + a.see.page));
    if (a.escalation) body.append(line("p", "What to do: " + a.escalation, "muted"));
    if (a.devices) body.append(line("p", "Say which one, for example: " +
                                    a.devices.map((x) => x.make + " " + x.model + " (" + x.room + ")").join("; ")));
  }
</script>
</body></html>"""


def create_app(machine, clinic_url=CLINIC_URL):
    app = Flask(__name__)

    @app.before_request
    def loopback_only():
        # the page and the API exist for this machine only: a foreign Host (DNS rebinding) or address is refused
        if request.host not in HOSTS or (request.remote_addr or "127.0.0.1") not in LOOPBACK:
            abort(403)

    @app.after_request
    def headers(resp):
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Content-Security-Policy"] = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline';"
                                                   " connect-src 'self'; frame-ancestors 'none'")
        return resp

    @app.route("/")
    def page():
        return render_template_string(PAGE, s=machine.snapshot(), clinic_url=clinic_url.rstrip("/"))

    @app.route("/confirm", methods=["POST"])
    def confirm():
        # only this page may answer: its own origin, as JSON (a cross-site page cannot send that without a preflight
        # nobody answers; get_json reads nothing else), for the id it was shown
        if request.headers.get("Origin") not in ORIGINS:
            abort(403)
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("id"), str) or not isinstance(body.get("decision"), str):
            abort(400)
        result = machine.decide(body["id"], body["decision"])
        return ("", 204) if result == "taken" else (jsonify({"result": result}), 409)

    @app.route("/status")
    def status():
        return jsonify(machine.snapshot())

    @app.route("/events")
    def events():
        def stream():
            snap = machine.snapshot()
            yield f"data: {json.dumps(snap)}\n\n"
            while True:
                nxt = machine.wait_change(snap["version"], HEARTBEAT_SECONDS)
                if nxt["version"] == snap["version"] and nxt["answer"] == snap["answer"]:
                    yield "event: ping\ndata: \n\n"   # the page's proof the service is still there
                    continue                   # (an answer that has just expired is sent, so the page drops it)
                snap = nxt
                yield f"data: {json.dumps(snap)}\n\n"
        return Response(stream(), mimetype="text/event-stream")

    return app
