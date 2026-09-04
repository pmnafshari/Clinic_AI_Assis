// The clinic assistant conversation.
//
// Progressive enhancement: without javascript the form posts to /assistant
// and the server renders the same answer. This upgrades that to a continuing
// conversation by posting each turn to /assistant/ask and appending the
// reply, so the page never reloads mid-conversation.
//
// The transcript lives in the DOM for this visit only. There is no server
// session and nothing is stored - a reload starts fresh, which is honest
// about what this page keeps.
(function () {
  "use strict";

  var root = document.querySelector("[data-agent]");
  if (!root) return;
  var form = root.querySelector("[data-agent-form]");
  var input = root.querySelector(".agent-input");
  var thread = root.querySelector("[data-agent-thread]");
  var status = root.querySelector("[data-agent-status]");
  var send = root.querySelector("[data-agent-send]");
  var sendLabel = root.querySelector("[data-agent-send-label]");
  var starters = root.querySelector("[data-agent-starters]");
  if (!form || !input || !thread) return;

  var SIGNIN = root.getAttribute("data-signin-href");

  // state is set in ONE place so the class, the word and the control state
  // can never disagree
  function setState(name, label) {
    root.dataset.state = name;
    if (status) status.textContent = label;
    var busy = name === "thinking";
    if (send) send.disabled = busy;
    input.disabled = busy;
    if (sendLabel && send) send.setAttribute("aria-busy", busy ? "true" : "false");
  }

  function turn(text, who, variant) {
    var p = document.createElement("p");
    p.className = "agent-turn agent-turn--" + who + (variant ? " agent-turn--" + variant : "");
    p.textContent = text;
    thread.appendChild(p);
    p.scrollIntoView({ block: "nearest", behavior: "smooth" });
    return p;
  }

  function ask(question) {
    if (!question) return;
    if (starters) starters.hidden = true;
    turn(question, "user");
    input.value = "";
    setState("thinking", root.dataset.thinkingLabel || "Thinking");

    var body = new FormData();
    body.append("question", question);

    fetch("/assistant/ask", { method: "POST", body: body })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        turn(data.text, "agent", data.state);
        if (data.state === "handoff" && SIGNIN) {
          var a = document.createElement("a");
          a.className = "ds-btn ds-btn-primary agent-signin";
          a.href = SIGNIN;
          a.textContent = root.dataset.signinLabel || "Sign in";
          thread.appendChild(a);
        }
        setState("ready", "Ready");
      })
      .catch(function () {
        // a real failure, said plainly. never a fabricated answer.
        turn("I could not reach the clinic information just now. Please try again, or call the clinic.",
             "agent", "error");
        setState("ready", "Ready");
      })
      .finally(function () { input.focus(); });
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    ask(input.value.trim());
  });

  if (starters) {
    starters.addEventListener("click", function (e) {
      var b = e.target.closest("[data-agent-starter]");
      if (b) ask(b.textContent.trim());
    });
  }

  setState("ready", "Ready");
})();

// --- voice -----------------------------------------------------------------
//
// Microphone -> MediaRecorder -> /assistant/voice -> transcript + reply +
// spoken audio. Every hop is real: the amplitude driving the presence ring
// comes from an AnalyserNode reading the actual stream, the transcript is
// what Deepgram returned, and the speech is the audio this server fetched.
//
// The browser holds no API key. It posts audio to this app and receives text
// and sound back; the vendors are only ever called server-side.
//
// Nothing is recorded to disk. The Blob lives in memory for one request.
(function () {
  "use strict";

  var root = document.querySelector("[data-agent]");
  if (!root) return;
  var thread = root.querySelector("[data-agent-thread]");
  var status = root.querySelector("[data-agent-status]");
  var mount = root.querySelector("[data-agent-voice]");
  if (!mount || !thread) return;

  var L = {
    listening: root.dataset.listeningLabel || "Listening",
    speaking: root.dataset.speakingLabel || "Speaking",
    thinking: root.dataset.thinkingLabel || "Thinking",
    start: root.dataset.voiceStartLabel || "Start voice conversation",
    stop: root.dataset.voiceStopLabel || "Stop",
    micDenied: root.dataset.micDeniedNote || "Microphone unavailable. You can still type."
  };

  var btn = mount.querySelector("[data-agent-mic]");
  var note = mount.querySelector("[data-agent-voice-note]");
  // the ring lives in the page header, NOT inside the voice control mount.
  // scoping this lookup to `mount` returned null, and meter() bails on its
  // first line when level is null - so the analyser was never read at all.
  // that, not the audio graph, is why every sample measured zero.
  var level = root.querySelector("[data-agent-level]");

  // ONE audio graph, built once and kept. The previous version called
  // createMediaElementSource on every turn - it may only be called once per
  // element, so the second call threw InvalidStateError, the catch swallowed
  // it, and no analyser was attached at all. Measured: 0 of 24 samples
  // non-zero during confirmed playback.
  //
  //   micSource  --(only while listening)--> analyser
  //   elSource   --------------------------> analyser
  //   elSource   --------------------------> destination   (keeps it audible)
  var audioCtx = null, analyser = null, raf = null;
  var elSource = null, micSource = null;
  var recorder = null, chunks = [], stream = null;
  var player = new Audio();
  var running = false;

  function say(text, label) { root.dataset.state = text; if (status) status.textContent = label; }

  function turn(text, who, variant) {
    var p = document.createElement("p");
    p.className = "agent-turn agent-turn--" + who + (variant ? " agent-turn--" + variant : "");
    p.textContent = text;
    thread.appendChild(p);
    p.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  function graph() {
    if (audioCtx) return audioCtx;
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 512;
    analyser.smoothingTimeConstant = 0.6;
    // the element source is created exactly once, here, and stays wired for
    // the life of the page - both to the analyser and to the speakers
    elSource = audioCtx.createMediaElementSource(player);
    elSource.connect(analyser);
    elSource.connect(audioCtx.destination);
    return audioCtx;
  }

  // an AudioContext created outside a user gesture starts suspended, and a
  // suspended context feeds the analyser silence
  function wake() {
    graph();
    if (audioCtx.state === "suspended") { return audioCtx.resume(); }
    return Promise.resolve();
  }

  // the ring scales with the REAL rms of whatever is currently connected. it
  // runs only while listening or speaking; at rest nothing is connected and
  // the value stays 0, so the ring does not move.
  function meter() {
    if (!analyser || !level) { raf = null; return; }
    var buf = new Uint8Array(analyser.fftSize);
    analyser.getByteTimeDomainData(buf);
    var sum = 0;
    for (var i = 0; i < buf.length; i++) { var v = (buf[i] - 128) / 128; sum += v * v; }
    var rms = Math.sqrt(sum / buf.length);
    level.style.setProperty("--level", Math.min(1, rms * 3.2).toFixed(3));
    root.dataset.level = rms.toFixed(4);   // read by the verification harness
    raf = requestAnimationFrame(meter);
  }

  function startMeter() { if (!raf) meter(); }

  function stopMeter() {
    if (raf) cancelAnimationFrame(raf);
    raf = null;
    if (level) level.style.setProperty("--level", "0");
    root.dataset.level = "0";
  }

  function micToAnalyser(on) {
    if (!micSource) return;
    try { on ? micSource.connect(analyser) : micSource.disconnect(analyser); } catch (e) { /* already */ }
  }

  function fail(msg) {
    stopMeter();
    say("error", "Error");
    if (note) { note.textContent = msg; note.hidden = false; }
    running = false;
    if (btn) btn.textContent = L.start;
  }

  function send(blob) {
    say("thinking", L.thinking);
    stopMeter();
    var body = new FormData();
    body.append("audio", blob, "turn.webm");
    fetch("/assistant/voice", { method: "POST", body: body })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok) throw new Error(res.j.error || "voice failed");
        if (res.j.transcript) turn(res.j.transcript, "user");
        turn(res.j.text, "agent", res.j.state);
        if (res.j.audio) { play(res.j.audio); } else { say("ready", "Ready"); listen(); }
      })
      .catch(function (e) { fail(e.message); });
  }

  function play(b64) {
    say("speaking", L.speaking);
    // the assistant is talking, so the microphone must not be what the ring
    // measures
    micToAnalyser(false);
    player.src = "data:audio/mpeg;base64," + b64;
    player.onended = function () { stopMeter(); say("ready", "Ready"); if (running) listen(); };
    wake().then(startMeter);
    player.play().catch(function () {
      // autoplay refused: the text is already on screen, so the conversation
      // is not lost - just not spoken
      stopMeter(); say("ready", "Ready");
    });
  }

  function listen() {
    if (!running || !stream) return;
    say("listening", L.listening);
    micToAnalyser(true);
    wake().then(startMeter);
    chunks = [];
    recorder = new MediaRecorder(stream);
    recorder.ondataavailable = function (e) { if (e.data.size) chunks.push(e.data); };
    recorder.onstop = function () {
      var blob = new Blob(chunks, { type: "audio/webm" });
      chunks = [];
      if (blob.size > 1200 && running) { send(blob); } else if (running) { listen(); }
    };
    recorder.start();
    // fixed-length turns: this transport is request/response, not streaming,
    // so a turn is a recording rather than a live socket
    setTimeout(function () { if (recorder && recorder.state === "recording") recorder.stop(); }, 6000);
  }

  function start() {
    if (!navigator.mediaDevices || !window.MediaRecorder) { return fail(L.micDenied); }
    navigator.mediaDevices.getUserMedia({ audio: true })
      .then(function (s) {
        stream = s;
        running = true;
        if (btn) btn.textContent = L.stop;
        if (note) note.hidden = true;
        graph();
        micSource = audioCtx.createMediaStreamSource(stream);
        // the greeting is spoken on this click, which is the gesture browsers
        // require before audio may play
        fetch("/assistant/voice/greeting")
          .then(function (r) { return r.ok ? r.json() : Promise.reject(new Error("greeting")); })
          .then(function (j) { if (j.audio) { play(j.audio); } else { listen(); } })
          .catch(function () { listen(); });
      })
      .catch(function () { fail(L.micDenied); });
  }

  function stop() {
    running = false;
    if (recorder && recorder.state === "recording") recorder.stop();
    if (stream) { stream.getTracks().forEach(function (t) { t.stop(); }); stream = null; }
    player.pause();
    stopMeter();
    say("ready", "Ready");
    if (btn) btn.textContent = L.start;
  }

  // the control only appears if the server says voice is armed - never a
  // button that cannot work
  fetch("/assistant/voice/status")
    .then(function (r) { return r.json(); })
    .then(function (j) {
      if (!j.available) { mount.hidden = true; return; }
      mount.hidden = false;
      if (btn) btn.addEventListener("click", function () { running ? stop() : start(); });
    })
    .catch(function () { mount.hidden = true; });
})();
