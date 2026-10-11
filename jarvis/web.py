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
  :root { --bg: #05060a; --ink: #f4ead9; --muted: #c4b096; --card: rgba(17, 19, 26, .92); --line: rgba(255, 150, 60, .28);
          --glow: #ff9a3c; --ok: #7ee2a8; --warn: #ffd27a; --bad: #ff8a7a; --info: #ffb468; }
  body { margin: 0; font: 16px/1.5 -apple-system, "Helvetica Neue", Arial, sans-serif; color: var(--ink);
         background: radial-gradient(ellipse at 50% 18%, #1a0f05 0%, var(--bg) 60%) fixed var(--bg); }
  main { max-width: 46rem; margin: 0 auto; padding: 16px 16px 32px; }
  .brand { font-weight: 700; letter-spacing: .35em; color: var(--glow); margin: 0; font-size: 15px; }
  .hud { position: relative; margin: 8px auto 4px; max-width: 480px; }
  .hud::before, .hud::after { content: ""; position: absolute; left: 4%; right: 4%; height: 3px; border-radius: 2px;
         background: linear-gradient(90deg, rgba(255,140,40,.7), rgba(255,140,40,.08) 60%, rgba(255,140,40,.5)); }
  .hud::before { top: 0; } .hud::after { bottom: 0; }
  #orb { display: block; width: 100%; aspect-ratio: 1 / 1; }
  .brand { text-align: center; }
  .status-block { text-align: center; margin: 4px 0 14px; }
  .status { font-size: 44px; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; margin: 0; }
  .status.Ready { color: var(--ok); } .status.Active { color: var(--glow); } .status.Sleep { color: var(--muted); }
  .why { margin: 2px 0 0; color: var(--muted); font-size: 17px; }
  .qa { max-width: 40rem; margin: 0 auto 18px; padding: 14px 18px; border-radius: 12px; background: rgba(0, 0, 0, .6);
        border: 1px solid var(--line); font-size: 20px; line-height: 1.45; min-height: 3.2em; }
  .qa .line { margin: 0 0 6px; } .qa .who { color: var(--glow); font-weight: 700; margin-right: 10px; }
  .qa .line:has(span:not(.who):empty) { display: none; }
  .qa:not(:has(.line span:not(.who):not(:empty))) { display: none; }
  .cite { font-size: 15px; color: var(--muted); } .cite p { margin: 2px 0; }
  .access { max-width: 40rem; margin: 0 auto 16px; padding: 12px 18px; border-radius: 12px; border: 1px solid var(--line);
            background: rgba(0, 0, 0, .5); text-align: center; }
  .access-title { font-weight: 700; color: var(--glow); margin: 0 0 8px; }
  .access label { display: inline-flex; flex-direction: column; text-align: left; margin: 0 6px 8px; color: var(--muted); }
  .access input[type=text], .access input:not([type]), .access input[type=password] { font: inherit; padding: 8px 10px;
            border-radius: 8px; border: 1px solid var(--line); background: #0d0f14; color: var(--ink); min-width: 12rem; }
  .access input[type=submit] { font: inherit; font-weight: 600; padding: 8px 18px; border-radius: 10px; cursor: pointer;
            border: 2px solid var(--glow); background: var(--glow); color: #1a0d00; min-height: 24px; }
  #signout input[type=submit] { background: transparent; color: var(--info); margin-left: 8px; }
  .access input:focus-visible { outline: 3px solid var(--ink); outline-offset: 2px; }
  .access p { margin: 0; } #signin-error:empty { display: none; }
  .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
  .more { margin-top: 8px; } .more summary { cursor: pointer; color: var(--muted); min-height: 24px; padding: 4px 0; }
  .more[open] summary { margin-bottom: 12px; }
  .card { background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 18px 20px; margin-bottom: 16px; }
  .state { font-size: 28px; font-weight: 700; letter-spacing: .08em; margin: 0; }
  .READY { color: var(--ok); } .ACTIVE, .CONFIRMING, .WAITING_FOR_CONFIRMATION { color: var(--info); }
  .AUTH_REQUIRED { color: var(--warn); } .DEGRADED, .UNREACHABLE { color: var(--bad); } .STARTING { color: var(--muted); }
  .WAITING_FOR_CONFIRMATION { font-size: 22px; overflow-wrap: anywhere; }
  .status { text-align: center; } .status p { margin: 4px 0; }
  #confirm-status:empty { display: none; }
  .ready-line { font-weight: 600; margin-top: 0; } .ready-line.ok { color: var(--ok); } .ready-line.no { color: var(--warn); }
  .source { margin: 4px 0 12px; } .source a { color: var(--info); display: inline-block; min-height: 24px; padding: 2px 0; }
  a { color: var(--info); }
  #confirm-status { font-weight: 600; }
  .muted { color: var(--muted); } ol { padding-left: 1.2rem; } code { font-size: 14px; }
  h2 { color: var(--glow); font-size: 18px; letter-spacing: .06em; }
  blockquote { margin: 12px 0; padding: 8px 14px; border-left: 4px solid var(--glow); background: rgba(255,140,40,.08); }
  .warn { color: var(--warn); font-weight: 600; }
  .heard { font-size: 20px; font-weight: 600; }
  .choices { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 12px; }
  .choices button { font: inherit; font-weight: 600; padding: 10px 18px; border-radius: 10px; cursor: pointer;
                    border: 2px solid var(--glow); }
  .choices .yes { background: var(--glow); color: #1a0d00; } .choices .no { background: transparent; color: var(--info); }
  .choices button:focus-visible { outline: 3px solid var(--ink); outline-offset: 2px; }
  .choices button:disabled { opacity: .6; cursor: default; }
  @media (prefers-reduced-motion: reduce) { .hud::before, .hud::after { opacity: .6; } }
</style></head>
<body><main data-clinic="{{ clinic_url }}">
  <p class="brand">JARVIS</p>
  <div class="hud"><canvas id="orb" aria-hidden="true"></canvas></div>
  <div class="status-block"><div aria-live="polite" aria-atomic="true">
    <h1 id="status" class="status {{ s.status.word }}">{{ s.status.word }}</h1>
    <p id="status-why" class="why">{{ s.status.why }}</p>
  </div></div>
  {% set u = s.access %}
  <form id="signin" class="access"{% if u %} hidden{% endif %}>
    <p class="access-title">Sign in to Jarvis - doctors only</p>
    <label>Username <input name="username" autocomplete="username" required></label>
    <label>Password <input name="password" type="password" autocomplete="current-password" required></label>
    <input type="submit" value="Sign in">
    <p id="signin-error" class="warn" role="alert"></p>
  </form>
  <form id="signout" class="access"{% if not u %} hidden{% endif %}>
    <p>Signed in as <strong id="access-who">{{ u.username if u else "" }}</strong> - <span id="access-until">{% if u
      and u.kind != "web" %}until 00:00{% elif u %}until the staff session ends{% endif %}</span>
      <input type="submit" value="Sign out"></p>
  </form>
  {% set a = s.answer %}
  <section id="qa" class="qa" aria-label="Question and answer">
    <p class="line"><span class="who">You</span><span id="cc-you">{{ a.heard if a and a.heard else "" }}</span></p>
    <p class="line"><span class="who">Jarvis</span><span id="cc-jarvis" aria-hidden="true"></span></p>
    <div id="qa-cite" class="cite"></div>
    <p id="qa-said" class="sr-only" aria-live="polite"></p>
  </section>
  <details id="more" class="more"><summary>Details</summary>
  <section class="card">
    <p id="ready" class="ready-line {{ 'ok' if s.ready.ok else 'no' }}">{{ s.ready.text }}</p>
    <p class="muted">Listening:</p>
    <p class="state {{ s.state }}" id="state">{{ s.state }}</p>
    <p id="meaning">{{ s.meaning }}</p>
    <p><strong>Why:</strong> <span id="reason">{{ s.reason }}</span></p>
    <p class="muted">Since <span id="since">{{ s.since }}</span> (UTC)</p>
    <p><strong>Clinic-guide answers:</strong> <span id="answers">{{ s.answers.detail }}</span></p>
    <p class="muted">Clinic link (needed for clinic-guide answers, not for listening):
      <span id="clinic">{{ s.clinic.detail }}</span></p>
  </section>
  <p id="confirm-status" class="card" role="status" aria-live="polite"></p>
  <section class="card" id="answer"{% if not a %} hidden{% endif %}>
    <h2 id="answer-title" tabindex="-1">Last question</h2>
    <p class="muted">{% if s.answers.demo %}Answers are shown and read aloud by this Mac's voice (synthetic demo only; a
      new "Hey Jarvis" or Pause stops it).{% else %}Answers are shown on screen, not spoken.{% endif %} They stay for two
      minutes and are kept nowhere.</p>
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
        this</em> (in the synthetic demo it is asked at once, and the answer says what was heard); nothing is recorded
        meanwhile, and saying the wake phrase again starts over; then the clinic-guide
        answer appears with the document and page it comes from (read aloud only in the synthetic demo); then back to
        READY</li>
      <li>AUTH_REQUIRED - needs a delegated staff session; nothing protected is said</li>
      <li>CONFIRMING - a spoken confirmation of an action that is already authorised</li>
      <li>DEGRADED - not available, with the reason (asleep, microphone denied or muted, engine missing, fault)</li>
    </ol>
  </section>
  </details>
</main>
<script>
  const CLINIC = document.querySelector("main").dataset.clinic;
  // --- the orb: particles on rings and a core, moving with the sound (the microphone while listening, Jarvis's own
  //     voice while it speaks); calm when READY, dim when DEGRADED or paused; less motion if the viewer asks for it
  const orb = document.getElementById("orb"), ctx = orb.getContext("2d");
  const REDUCE = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  const MOODS = {READY: [255, 140, 40, .6], ACTIVE: [255, 190, 90, 1], WAITING_FOR_CONFIRMATION: [255, 170, 70, .85],
                 CONFIRMING: [255, 170, 70, .85], AUTH_REQUIRED: [255, 200, 110, .6], STARTING: [170, 110, 60, .35],
                 DEGRADED: [150, 70, 60, .22], UNREACHABLE: [90, 90, 90, .18], PAUSED: [150, 70, 60, .22]};
  let level = 0, target = 0, mood = "STARTING";
  const PARTS = [];
  for (let i = 0; i < 720; i++) {
    const ring = Math.random() < .6 ? 1 : (Math.random() < .55 ? .66 : .38);
    PARTS.push({a: Math.random() * 6.283, r: ring * (.88 + Math.random() * .24), s: (Math.random() - .5) * (2.2 - ring),
                l: 1 + Math.random() * 7, w: Math.random()});
  }
  function fit() {
    const d = window.devicePixelRatio || 1, w = orb.clientWidth || 360;
    orb.width = w * d; orb.height = w * d; ctx.setTransform(d, 0, 0, d, 0, 0);
  }
  window.addEventListener("resize", fit); fit();
  function frame(t) {
    level += (target - level) * .22;
    const w = orb.clientWidth || 360, c = w / 2, R = w * .38, amp = level;
    const [r, g, b, glow] = MOODS[mood] || MOODS.READY;
    const motion = REDUCE ? .15 : 1;
    ctx.globalCompositeOperation = "destination-out";      // fade the last frame, keeping the canvas see-through
    ctx.fillStyle = "rgba(0, 0, 0, 0.3)"; ctx.fillRect(0, 0, w, w);
    ctx.globalCompositeOperation = "lighter";
    for (const p of PARTS) {
      p.a += p.s * .003 * motion * (1 + amp * 5);
      const rr = R * p.r * (1 + amp * .32 * Math.sin(t / 170 + p.w * 14) * motion);
      const x = c + Math.cos(p.a) * rr, y = c + Math.sin(p.a) * rr, len = p.l * (1 + amp * 3);
      ctx.strokeStyle = "rgba(" + r + "," + g + "," + b + "," + ((.2 + .65 * p.w) * glow) + ")";
      ctx.lineWidth = .8 + amp * 1.6 * p.w;
      ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x - Math.sin(p.a) * len, y + Math.cos(p.a) * len); ctx.stroke();
    }
    for (let k = 0; k < 3; k++) {
      const s0 = (t / (800 + k * 350)) * motion + k * 2.1;
      ctx.strokeStyle = "rgba(255, 205, 130," + (.22 * glow + amp * .35) + ")"; ctx.lineWidth = 1.4;
      ctx.beginPath(); ctx.arc(c, c, R * (.42 + k * .13) * (1 + amp * .18), s0, s0 + 1.5 + amp * 2.2); ctx.stroke();
    }
    const core = R * (.16 + amp * .24);
    const grad = ctx.createRadialGradient(c, c, 0, c, c, core * 2.3);
    grad.addColorStop(0, "rgba(255, 236, 180," + Math.min(1, .5 * glow + amp * .45) + ")");
    grad.addColorStop(.4, "rgba(" + r + "," + g + "," + b + "," + (.35 * glow + amp * .2) + ")");
    grad.addColorStop(1, "rgba(0, 0, 0, 0)");
    ctx.fillStyle = grad; ctx.beginPath(); ctx.arc(c, c, core * 2.3, 0, 6.283); ctx.fill();
    window.requestAnimationFrame(frame);
  }
  window.requestAnimationFrame(frame);
  // --- the question and the answer (the captions): what Jarvis heard, then what it says, word by word with its voice
  let fullAnswer = "", speakingId = null;
  function caption(who, text) {
    const el = document.getElementById("cc-" + who);
    if (el.textContent !== text) el.textContent = text || "";
  }
  function spoken(a) {          // the same words as the voice (jarvis/speech.py words())
    if (!a || a.outcome === "confirm") return "";
    if (a.outcome === "note_confirm") return "Save this note? \u201c" + a.text + "\u201d - pick the patient below.";
    if (a.outcome === "answer") {
      const parts = [];
      for (const c of a.citations || []) parts.push(c.language && c.language !== "en" ? "The answer is on screen, in " +
                                                     (c.language === "it" ? "Italian." : "another language.") : c.passage);
      for (const w of a.warnings || []) parts.push("Warning: " + w.text);
      return parts.join(" ");
    }
    if (a.title && a.message && a.message.startsWith(a.title)) return a.message;
    return [a.title, a.message].filter(Boolean).join(" ");
  }
  function qa(a) {
    caption("you", a && a.heard ? a.heard : "");
    fullAnswer = spoken(a);
    caption("jarvis", speakingId === null ? fullAnswer : "");
    const cite = document.getElementById("qa-cite");
    cite.replaceChildren();
    for (const c of (a && a.citations) || []) cite.append(sourceLink(c.source_id, c.page, c.title + ", page " + c.page));
    if (a && a.outcome === "note_confirm") cite.append(noteChoices(a));
    if (a && a.outcome === "note_saved" && a.patient) cite.append(recordLink(a.patient));
    document.getElementById("qa-said").textContent = fullAnswer;      // read once by a screen reader
  }
  function live() {
    fetch("/live").then((r) => r.json()).then((v) => {
      mood = lost ? "UNREACHABLE" : (v.paused ? "PAUSED" : v.state);
      if (v.voice) {
        speakingId = v.voice.id;
        target = v.voice.level;
        const words = fullAnswer.split(" ");
        caption("jarvis", words.slice(0, Math.ceil(words.length * Math.min(1, v.voice.progress + .06))).join(" "));
      } else {
        if (speakingId !== null) { speakingId = null; caption("jarvis", fullAnswer); }
        target = Math.min(1, v.mic * 2.5);
      }
    }).catch(() => { target = 0; mood = "UNREACHABLE"; });
  }
  setInterval(live, 90);
  const STALE_MS = 4000;           // two heartbeats missed
  const CHECK_MS = 250;            // a frozen service shows within STALE_MS + CHECK_MS (4.25 s; a 1 s check reached 4.96 s)
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
    setText("status", "Sleep"); document.getElementById("status").className = "status Sleep";
    setText("status-why", "Jarvis is not running");
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
  // --- a doctor signs Jarvis in (2026-10-11): the page passes the username and password to Jarvis, which passes
  //     them to the clinic; nothing is kept in the page either - the password field is cleared at once
  const SIGNIN = {credentials: "Wrong username or password.", not_doctor: "Only a doctor can sign Jarvis in.",
                  password_change: "Change your password in the clinic app first.",
                  clinic: "The clinic app is not reachable - is it running?"};
  function setAccess(u) {
    document.getElementById("signin").hidden = !!u;
    document.getElementById("signout").hidden = !u;
    setText("access-who", u ? u.username : "");
    setText("access-until", !u ? "" : u.kind === "web" ? "until the staff session ends" : "until 00:00");
  }
  document.getElementById("signin").addEventListener("submit", (e) => {
    e.preventDefault();
    const f = e.target, err = document.getElementById("signin-error");
    const body = JSON.stringify({username: f.username.value, password: f.password.value});
    f.password.value = "";
    err.textContent = "";
    fetch("/signin", {method: "POST", headers: {"Content-Type": "application/json"}, body: body})
      .then((r) => r.ok ? r.json().then(setAccess) :
            r.json().catch(() => ({})).then((b) => { err.textContent = SIGNIN[b.error] || "Not signed in."; }))
      .catch(() => { err.textContent = "Jarvis is not reachable."; });
  });
  document.getElementById("signout").addEventListener("submit", (e) => {
    e.preventDefault();
    fetch("/signout", {method: "POST"}).then(() => setAccess(null)).catch(() => {});
  });
  function render(s) {
    setAccess(s.access);
    setText("status", s.status.word);
    document.getElementById("status").className = "status " + s.status.word;
    setText("status-why", s.status.why);
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
  setInterval(() => { if (Date.now() - lastSeen > STALE_MS) unreachable("no word from the Jarvis service"); }, CHECK_MS);
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
  // J03: a note waits on screen - one button per patient the name matched (same names told apart by codice
  // fiscale), and Discard; only a click saves
  function noteChoices(a) {
    const box = line("div", "", "choices");
    for (const c of a.choices || []) {
      const b = line("button", "Save to " + c.name + " \u00b7 " + c.cf, "yes");
      b.type = "button";
      b.addEventListener("click", () => decide(a.id, "save:" + c.cf, box));
      box.append(b);
    }
    const no = line("button", "Discard", "no");
    no.type = "button";
    no.addEventListener("click", () => decide(a.id, "discard", box));
    box.append(no);
    return box;
  }
  function recordLink(p) {     // the record in the clinic app, opened there under the viewer's own sign-in
    const el = line("p", "", "source"), link = line("a", "Open " + p.name + "'s record");
    link.href = CLINIC + "/patients/" + encodeURIComponent(p.cf);
    link.target = "_blank"; link.rel = "noopener noreferrer";
    el.append(link);
    return el;
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
    qa(a);
    if (!a) return;
    if (a.title) body.append(line("h3", a.title));
    if (a.outcome === "confirm") {
      document.getElementById("more").open = true;      // the Yes/No card is under Details: show it
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


def create_app(machine, clinic_url=CLINIC_URL, speaker=None, access=None):
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

    @app.route("/listening", methods=["POST"])
    def listening():
        # pause or resume listening (P28, the Clinic Workspace app); the same checks as /confirm
        if request.headers.get("Origin") not in ORIGINS:
            abort(403)
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("paused"), bool):
            abort(400)
        machine.set_paused(body["paused"])
        return "", 204

    @app.route("/signin", methods=["POST"])
    def signin():
        # a doctor signs Jarvis in (owner, 2026-10-11): only this page may (Origin, as /confirm); the username and
        # password go straight to the clinic and are kept nowhere here
        if request.headers.get("Origin") not in ORIGINS:
            abort(403)
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("username"), str) or \
                not isinstance(body.get("password"), str) or access is None:
            abort(400)
        from jarvis.clinic import LinkDown, LinkRefused, Refused
        try:
            who = access.signin(body["username"].strip(), body["password"])
        except Refused as e:
            return jsonify({"error": e.code, "message": str(e)}), 403
        except (LinkRefused, LinkDown) as e:
            return jsonify({"error": "clinic", "message": str(e)}), 503
        machine.set_access(who)
        return jsonify({"username": who["username"], "expires_at": who["expires_at"]})

    @app.route("/signout", methods=["POST"])
    def signout():
        if request.headers.get("Origin") not in ORIGINS:
            abort(403)
        if access is not None:
            from jarvis.clinic import LinkDown, LinkRefused
            try:
                access.signout()
            except (LinkRefused, LinkDown):
                pass
        machine.set_access(None)
        return "", 204

    @app.route("/live")
    def live():
        # for the page's animation and captions: the microphone's loudness now and, while Jarvis speaks, its own
        # voice's loudness, words and progress (polled several times a second; no history, nothing kept)
        return jsonify({"state": machine.state, "paused": machine.paused, "mic": round(machine.mic_level, 3),
                        "voice": speaker.live() if speaker else None})

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
