"""Jarvis's own page and status API on 127.0.0.1:5020. Loopback only. The page shows the truth; it never starts a
conversation (Jarvis is hands-free) and closing it changes nothing."""
import json

from flask import Flask, Response, abort, jsonify, render_template_string, request

PORT = 5020
HOSTS = {"127.0.0.1", "localhost", f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
LOOPBACK = {"127.0.0.1", "::1"}
HEARTBEAT_SECONDS = 15

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
  .READY { color: var(--ok); } .ACTIVE, .CONFIRMING { color: var(--info); } .AUTH_REQUIRED { color: var(--warn); }
  .DEGRADED { color: var(--bad); } .STARTING { color: var(--muted); }
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
<body><main>
  <p class="muted">Jarvis · local voice companion · this page only shows it; Jarvis keeps running when it is closed</p>
  <section class="card" aria-live="polite" aria-atomic="true">
    <h1 class="state {{ s.state }}" id="state">{{ s.state }}</h1>
    <p id="meaning">{{ s.meaning }}</p>
    <p><strong>Why:</strong> <span id="reason">{{ s.reason }}</span></p>
    <p class="muted">Since <span id="since">{{ s.since }}</span> (UTC)</p>
    <p class="muted">Clinic link: <span id="clinic">{{ s.clinic.detail }}</span>
      (needed for answers and anything about a patient, not for listening)</p>
  </section>
  {% set a = s.answer %}
  <section class="card" id="answer" aria-live="polite"{% if not a %} hidden{% endif %}>
    <h2>Last question</h2>
    <p class="muted">Answers are shown here on screen, not spoken. They stay for two minutes and are kept nowhere.</p>
    <div id="answer-body">{% if a and a.outcome == "confirm" %}
      <h3>Did I hear you right?</h3>
      <p class="heard">{{ a.heard }}</p>
      <p class="muted">Nothing is asked until you confirm. If you do not, it is discarded after {{ a.seconds }} seconds.</p>
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
      <li>ACTIVE - one request after a short tone; what was heard appears here first, and nothing is asked until you
        press <em>Yes, ask this</em>; then the clinic-guide answer appears with the document and page it comes from
        (nothing is spoken back); then back to READY</li>
      <li>AUTH_REQUIRED - needs a delegated staff session; nothing protected is said</li>
      <li>CONFIRMING - a spoken confirmation of an action that is already authorised</li>
      <li>DEGRADED - not available, with the reason (asleep, microphone denied or muted, engine missing, fault)</li>
    </ol>
  </section>
</main>
<script>
  const es = new EventSource("/events");
  es.onmessage = (e) => {
    const s = JSON.parse(e.data);
    const h = document.getElementById("state");
    h.textContent = s.state; h.className = "state " + s.state;
    document.getElementById("meaning").textContent = s.meaning;
    document.getElementById("reason").textContent = s.reason;
    document.getElementById("since").textContent = s.since;
    document.getElementById("clinic").textContent = s.clinic.detail;
    showAnswer(s.answer);
  };
  // text only: what was heard and what the guides say are never treated as markup
  function line(tag, text, cls) {
    const el = document.createElement(tag);
    el.textContent = text;
    if (cls) el.className = cls;
    return el;
  }
  function decide(id, decision, box) {
    for (const b of box.querySelectorAll("button")) b.disabled = true;
    fetch("/confirm", {method: "POST", headers: {"Content-Type": "application/json"},
                       body: JSON.stringify({id: id, decision: decision})});
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
  let shownId = null;
  function showAnswer(a) {
    const card = document.getElementById("answer"), body = document.getElementById("answer-body");
    card.hidden = !a;
    if (a && a.outcome === "confirm" && a.id === shownId) return;   // the same question: keep the buttons as they are
    shownId = a && a.outcome === "confirm" ? a.id : null;
    body.replaceChildren();
    if (!a) return;
    if (a.outcome === "confirm") {
      body.append(line("h3", "Did I hear you right?"), line("p", a.heard, "heard"),
                  line("p", "Nothing is asked until you confirm. If you do not, it is discarded after " + a.seconds +
                       " seconds.", "muted"), choices(a.id));
      return;
    }
    if (a.heard) body.append(line("p", "Heard: " + a.heard));
    if (a.asked_as && a.asked_as !== a.heard) body.append(line("p", "Asked as: " + a.asked_as));
    for (const c of a.citations || []) {
      body.append(line("blockquote", c.passage));
      body.append(line("p", c.title + (c.edition ? ", edition " + c.edition : "") + ", page " + c.page +
                       " - verified against the page", "muted"));
    }
    for (const w of a.warnings || []) body.append(line("p", w.text + " (page " + w.page + ")", "warn"));
    if (a.message) body.append(line("p", a.message));
    if (a.escalation) body.append(line("p", a.escalation, "muted"));
    if (a.devices) body.append(line("p", "Say which one, for example: " +
                                    a.devices.map((d) => d.make + " " + d.model + " (" + d.room + ")").join("; ")));
  }
</script>
</body></html>"""


def create_app(machine):
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
        return render_template_string(PAGE, s=machine.snapshot())

    @app.route("/confirm", methods=["POST"])
    def confirm():
        # only this page may answer: its own origin, as JSON (a cross-site page cannot send that without a preflight
        # nobody answers; get_json reads nothing else), for the id it was shown
        if request.headers.get("Origin") not in ORIGINS:
            abort(403)
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("id"), str) or not isinstance(body.get("decision"), str):
            abort(400)
        return ("", 204) if machine.decide(body["id"], body["decision"]) else ("", 409)

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
                    yield ": keep-alive\n\n"
                    continue                   # (an answer that has just expired is sent, so the page drops it)
                snap = nxt
                yield f"data: {json.dumps(snap)}\n\n"
        return Response(stream(), mimetype="text/event-stream")

    return app
