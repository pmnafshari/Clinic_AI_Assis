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
    micDenied: root.dataset.micDeniedNote || "Microphone access was refused. You can still type.",
    micUnavailable: root.dataset.micUnavailableNote || "No microphone was available. You can still type.",
    autoplay: root.dataset.autoplayNote || "Your browser keeps sound off until you interact with the page. Click anywhere to turn it on."
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
  // getUserMedia is in flight. autostart() fires off the status fetch while the
  // button is already live, so without this a click in that window opens a
  // SECOND stream, overwrites `stream`, and leaves the first one running with
  // nothing holding its tracks - stop() can no longer release it and the
  // browser keeps showing the recording indicator. measured: 2 calls.
  var starting = false;
  var waitingForGesture = false;

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

  // a refused AUTO start is not an error - the visitor never asked for it on
  // this page load. say what happened, leave the button armed for a real
  // click, and keep the typed composer exactly as it was.
  function offer(msg) {
    stopMeter();
    say("ready", "Ready");
    if (note) { note.textContent = msg; note.hidden = false; }
    running = false;
    if (btn) btn.textContent = L.start;
  }

  // browsers keep audio off until the page has been interacted with. when
  // play() is refused we wait for the first real gesture and resume the
  // context on it, so the NEXT reply is spoken. this is what makes the note
  // honest: it says "click anywhere", and clicking anywhere is what fixes it.
  //
  // the pending clip is deliberately NOT replayed. by the time a gesture
  // arrives the microphone is usually recording again, and playing the old
  // greeting into a live turn would put the assistant's own voice through
  // deepgram as if the visitor had said it.
  function armGesture() {
    if (waitingForGesture) return;
    waitingForGesture = true;
    function go() {
      document.removeEventListener("click", go, true);
      document.removeEventListener("keydown", go, true);
      waitingForGesture = false;
      if (note) note.hidden = true;
      wake();
    }
    document.addEventListener("click", go, true);
    document.addEventListener("keydown", go, true);
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
      // autoplay refused. the reply is already on screen so nothing is lost
      // but the sound - say so, offer a remedy that works, and keep the
      // microphone turn going instead of ending the session without a word.
      stopMeter();
      say("ready", "Ready");
      if (note) { note.textContent = L.autoplay; note.hidden = false; }
      armGesture();
      if (running) listen();
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

  // auto: this start was not asked for by a click, so a refusal is reported
  // softly by offer() rather than as an error
  function start(auto) {
    // one attempt at a time. a second stream here is not a cosmetic problem:
    // it is a microphone left open that nothing can close.
    if (running || starting) return;
    var refuse = auto ? offer : fail;
    if (!navigator.mediaDevices || !window.MediaRecorder) { return refuse(L.micUnavailable); }
    starting = true;
    navigator.mediaDevices.getUserMedia({ audio: true })
      .then(function (s) {
        starting = false;
        stream = s;
        running = true;
        if (btn) btn.textContent = L.stop;
        if (note) note.hidden = true;
        graph();
        micSource = audioCtx.createMediaStreamSource(stream);
        // a click is the gesture browsers want before audio may play. on an
        // auto start there is no gesture, so play() may be refused - it
        // reports that and carries on listening.
        fetch("/assistant/voice/greeting")
          .then(function (r) { return r.ok ? r.json() : Promise.reject(new Error("greeting")); })
          .then(function (j) { if (j.audio) { play(j.audio); } else { listen(); } })
          .catch(function () { listen(); });
      })
      // a refusal and a missing device are different facts and need different
      // remedies. telling someone who has no microphone attached that they
      // "refused access" sends them to a permission dialog that will not help.
      .catch(function (e) {
        starting = false;
        var denied = e && (e.name === "NotAllowedError" || e.name === "SecurityError");
        refuse(denied ? L.micDenied : L.micUnavailable);
      });
  }

  // opening the page is taken as the intent to talk, so the conversation
  // starts without waiting for the click. it is only ever an ATTEMPT: if the
  // microphone is refused the page stays exactly as usable as before.
  function autostart() {
    if (!navigator.permissions || !navigator.permissions.query) return start(true);
    navigator.permissions.query({ name: "microphone" })
      .then(function (p) {
        // already denied: the prompt will not appear, so asking would only
        // produce a silent rejection
        if (p.state === "denied") return offer(L.micDenied);
        start(true);
      })
      .catch(function () { start(true); });
  }

  function stop() {
    running = false;
    if (recorder && recorder.state === "recording") recorder.stop();
    // drop the old node before the stream goes: a restart builds a new
    // source, and without this the dead one stays wired to the analyser and
    // the next one stacks on top of it
    micToAnalyser(false);
    micSource = null;
    if (stream) { stream.getTracks().forEach(function (t) { t.stop(); }); stream = null; }
    player.pause();
    stopMeter();
    say("ready", "Ready");
    if (btn) btn.textContent = L.start;
  }

  // bound once, unconditionally. binding it inside the status callback meant
  // that whenever the button was on screen without voice armed it carried no
  // listener at all, so clicking it did nothing - visibly a control, actually
  // inert. what keeps a dead button off the page is hiding the mount, not
  // withholding its handler.
  if (btn) btn.addEventListener("click", function () { running ? stop() : start(false); });

  // the control only appears if the server says voice is armed - never a
  // button that cannot work
  fetch("/assistant/voice/status")
    .then(function (r) { return r.json(); })
    .then(function (j) {
      if (!j.available) { mount.hidden = true; return; }
      mount.hidden = false;
      autostart();
    })
    .catch(function () { mount.hidden = true; });
})();
