"""Jarvis's own page and status API on 127.0.0.1:5020. Loopback only. The page shows the truth; it never starts a
conversation (Jarvis is hands-free) and closing it changes nothing."""
import json

from flask import Flask, Response, abort, jsonify, render_template_string, request

PORT = 5020
HOSTS = {"127.0.0.1", "localhost", f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
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
</style></head>
<body><main>
  <p class="muted">Jarvis · local voice companion · this page only shows it; Jarvis keeps running when it is closed</p>
  <section class="card" aria-live="polite" aria-atomic="true">
    <h1 class="state {{ s.state }}" id="state">{{ s.state }}</h1>
    <p id="meaning">{{ s.meaning }}</p>
    <p><strong>Why:</strong> <span id="reason">{{ s.reason }}</span></p>
    <p class="muted">Since <span id="since">{{ s.since }}</span> (UTC)</p>
  </section>
  <section class="card">
    <h2>How Jarvis works</h2>
    <p>Jarvis starts when you log in to this computer and listens only for its wake phrase. You never click to talk.
    Anything about a patient needs a staff member's own signed-in session, delegated to this device in the clinic app
    (<em>Jarvis</em> in the sidebar). A voice, a name or a spoken "yes" never proves who you are.</p>
    <ol class="muted">
      <li>STARTING - checking the clinic link, the microphone and the engines</li>
      <li>READY - only the wake phrase is listened for; nothing is recorded or sent</li>
      <li>ACTIVE - one request; then back to READY</li>
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
  };
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
                if nxt["version"] == snap["version"]:
                    yield ": keep-alive\n\n"
                    continue
                snap = nxt
                yield f"data: {json.dumps(snap)}\n\n"
        return Response(stream(), mimetype="text/event-stream")

    return app
