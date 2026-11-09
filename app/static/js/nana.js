// Nana's card on the doctor's pages (owner, 2026-11-09). Plain script, no framework, after app.js (uiText).
//
// What Nana hears and answers comes from its own service on this computer (data-service, its live stream): the card
// prints what was asked, thinks, then prints the answer, with a button wherever the answer points at a record or a
// page. Turning it on hands this dentist's own session to this computer's Nana through the clinic app (data-card) and
// opens Nana's microphone; off closes the microphone and takes the session back. Standby keeps listening as a small
// orb and opens by itself when Nana is spoken to. The conversation stays in this tab only (sessionStorage), until the
// tab is closed or Nana is turned off; the last choice of on, standby or off is remembered in this browser.
(function () {
  var root = document.getElementById("nana");
  if (!root || typeof uiText !== "function") return;
  var SERVICE = root.dataset.service, CARD = root.dataset.card, USER = root.dataset.user, PHRASE = root.dataset.phrase;
  var PREF = "nana-mode:" + USER, LOG = "nana-log:" + USER, SINCE = "nana-since:" + USER, MAX = 30, STALE_MS = 8000;
  var WAITING = {confirm: 1, note_confirm: 1, edit_confirm: 1, patient_pick: 1};
  var PATIENTS = {patient_open: 1, patient_pick: 1, patient_next: 1, edit_confirm: 1, edit_saved: 1};
  // the service's own titles and weekdays, in the page's language
  var TITLES = {
    "Next appointment": uiText("Next appointment"), "Patient record": uiText("Patient record"),
    "Which patient?": uiText("Which patient?"), "Note saved": uiText("Note saved"), "Not saved": uiText("Not saved"),
    "Phone number saved": uiText("Phone number saved"), "Not opened": uiText("Not opened"),
    "Not changed": uiText("Not changed"), "Change the phone number?": uiText("Change the phone number?"),
    "Sign-in needed": uiText("Sign-in needed"), "No patient by that name": uiText("No patient by that name"),
    "Several patients match": uiText("Several patients match"), "Say the note again": uiText("Say the note again")
  };
  var DAYS = {
    Monday: uiText("Monday"), Tuesday: uiText("Tuesday"), Wednesday: uiText("Wednesday"), Thursday: uiText("Thursday"),
    Friday: uiText("Friday"), Saturday: uiText("Saturday"), Sunday: uiText("Sunday")
  };
  var REFUSED = {
    expired: uiText("Too late: the time to choose had passed, so nothing was done."),
    gone: uiText("This is no longer waiting - nothing was done."),
    ambiguous: uiText("More than one choice reached Nana, so nothing was done.")
  };
  var card = document.getElementById("nana-card"), pill = document.getElementById("nana-pill"),
    stateEl = document.getElementById("nana-state"), whyEl = document.getElementById("nana-why"),
    logEl = document.getElementById("nana-log"), noteEl = document.getElementById("nana-note"),
    powerBtn = root.querySelector('[data-nana="power"]'), standbyBtn = root.querySelector('[data-nana="standby"]');
  var CSRF = (function () {
    try { return JSON.parse(document.body.getAttribute("hx-headers"))["X-CSRFToken"]; } catch (e) { return ""; }
  })();
  var mode = "off", es = null, snap = null, device = null, lastSeen = 0, offline = false, busy = false;
  var liveTimer = null, noteTimer = null, shownPhase = "", entries = [];
  var drawn = null;         // the messages already on the card: only a new one comes in with a movement

  // --- browser storage: may be missing (private windows) - the card works without it ---------------------------------
  function box(session) { try { return session ? window.sessionStorage : window.localStorage; } catch (e) { return null; } }
  function load(key, session) {
    try { var s = box(session), v = s && s.getItem(key); return v ? JSON.parse(v) : null; } catch (e) { return null; }
  }
  function keep(key, value, session) {
    try {
      var s = box(session);
      if (!s) return;
      if (value === null) s.removeItem(key); else s.setItem(key, JSON.stringify(value));
    } catch (e) { /* the card goes on without it */ }
  }
  (function forgetOthers() {      // another person's conversation in this tab is never shown
    try {
      var s = window.sessionStorage;
      for (var i = s.length - 1; i >= 0; i--) {
        var k = s.key(i);
        if (k && (k.indexOf("nana-log:") === 0 && k !== LOG || k.indexOf("nana-since:") === 0 && k !== SINCE)) s.removeItem(k);
      }
    } catch (e) { /* nothing kept */ }
  })();
  entries = load(LOG, true) || [];
  // the card shows only the interactions after it was turned on - never the last answer still on Nana's screen,
  // which may be another doctor's
  var since = load(SINCE, true);
  function from(s) { since = s.turns || 0; keep(SINCE, since, true); }

  // --- small builders: text is always text, never markup -----------------------------------------------------------
  function mk(tag, text, cls) {
    var n = document.createElement(tag);
    if (text) n.textContent = text;
    if (cls) n.className = cls;
    return n;
  }
  function button(label, kind, onClick, aria) {
    var b = mk("button", label, "btn btn-sm btn-" + kind);
    b.type = "button";
    if (aria) b.setAttribute("aria-label", aria);
    b.addEventListener("click", onClick);
    return b;
  }
  function go(label, url, aria, before) {       // somewhere in the clinic app: a button, as every page's (owner rule)
    return button(label, "light", function () {
      if (before) { before(); setTimeout(function () { window.location.assign(url); }, 150); }
      else window.location.assign(url);
    }, aria);
  }
  function row(items) {
    var r = mk("div", "", "nana-actions");
    items.forEach(function (i) { if (i) r.append(i); });
    return r;
  }
  function record(cf) { return "/patients/" + encodeURIComponent(cf); }
  function say(text) {
    clearTimeout(noteTimer);
    noteEl.textContent = text || "";
    noteEl.hidden = !text;
    if (text) noteTimer = setTimeout(function () { noteEl.hidden = true; }, 9000);
  }

  // --- talking to the clinic app and to Nana ------------------------------------------------------------------------
  function post(url, body, own) {
    var headers = {"Content-Type": "application/json"};
    if (own) headers["X-CSRFToken"] = CSRF;
    return fetch(url, {method: "POST", headers: headers, body: JSON.stringify(body || {}),
                       credentials: own ? "same-origin" : "omit"});
  }
  function status() {
    return fetch(SERVICE + "/status", {credentials: "omit"}).then(function (r) {
      if (!r.ok) throw new Error("status");
      return r.json();
    });
  }
  var NOT_RUNNING = uiText("Nana is not running on this computer.");
  function turnOn(target) {
    if (busy) return;
    busy = true;
    say("");
    root.classList.add("nana-waking");
    status().catch(function () { throw NOT_RUNNING; }).then(function (s) {
      device = s.clinic && s.clinic.device_id;
      from(s);
      if (!device) throw uiText("This computer is not registered for Nana. An administrator registers it under Admin > Nana devices.");
      return post(CARD, {on: true, device_id: device}, true).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (b) {
          if (!r.ok) throw (b.error || uiText("Nana could not act for you."));
        });
      });
    }).then(function () {
      return post(SERVICE + "/listening", {paused: false}).catch(function () { throw NOT_RUNNING; });
    }).then(function () {
      post(SERVICE + "/recheck").catch(function () { /* the next check, within 30 s */ });
      keep(PREF, target);
      connect();
      setMode(target, true);
    }).catch(function (why) {
      setMode("off", false);
      say(typeof why === "string" ? why : NOT_RUNNING);
    }).then(function () {
      busy = false;
      root.classList.remove("nana-waking");
    });
  }
  function turnOff() {
    var d = device;
    post(SERVICE + "/listening", {paused: true}).catch(function () { /* Nana is not running: nothing listens */ });
    (d ? post(CARD, {on: false, device_id: d}, true) : Promise.resolve()).catch(function () { /* ends at sign-out */ })
      .then(function () { post(SERVICE + "/recheck").catch(function () { /* nothing to recheck */ }); });
    disconnect();
    entries = [];
    keep(LOG, null, true);
    keep(SINCE, null, true);
    since = null;
    keep(PREF, "off");
    renderLog();
    setMode("off", true);
  }
  function connect() {
    if (es) return;
    lastSeen = Date.now();
    es = new EventSource(SERVICE + "/events");
    es.onmessage = function (e) {
      lastSeen = Date.now();
      try { handle(JSON.parse(e.data)); } catch (x) { /* a bad message is skipped */ }
    };
    es.addEventListener("ping", function () { lastSeen = Date.now(); setOffline(false); });
    es.onerror = function () { setOffline(true); };
  }
  function disconnect() {
    if (es) { es.close(); es = null; }
    snap = null;
    stopLive();
    setOffline(false);
  }
  setInterval(function () { if (es && Date.now() - lastSeen > STALE_MS) setOffline(true); }, 3000);
  function setOffline(on) {
    if (offline === on) return;
    offline = on;
    paint();
  }

  // --- what the card says about Nana now ----------------------------------------------------------------------------
  function phase() {
    if (mode === "off") return "off";
    if (offline || !snap) return offline ? "offline" : "starting";
    if (snap.paused) return "paused";
    if (snap.state === "ACTIVE") {
      if (snap.asked && snap.asked.turn === snap.turn) return "thinking";
      return /^(heard the wake phrase|follow-up)/.test(snap.reason || "") ? "listening" : "thinking";
    }
    return {READY: "ready", STARTING: "starting", WAITING_FOR_CONFIRMATION: "check", CONFIRMING: "pick",
            AUTH_REQUIRED: "thinking", DEGRADED: "unavailable"}[snap.state] || "unavailable";
  }
  function paint() {
    var p = phase(), word = "", why = "";
    if (p === "off") { word = uiText("Off"); why = uiText("Switched off - the microphone is closed."); }
    else if (p === "offline") { word = uiText("Offline"); why = NOT_RUNNING; }
    else if (p === "paused") { word = uiText("Paused"); why = uiText("Switched off - the microphone is closed."); }
    else if (p === "starting") { word = uiText("Starting"); why = uiText("Opening the microphone."); }
    else if (p === "listening") { word = uiText("Listening"); }
    else if (p === "thinking") { word = uiText("Thinking"); }
    else if (p === "check") { word = uiText("Check"); why = uiText("Check what I heard below."); }
    else if (p === "pick") { word = uiText("Pick"); why = uiText("Pick below - nothing is done until you do."); }
    else if (p === "unavailable") { word = uiText("Not available"); why = uiText("Nana cannot listen now - its own page says why."); }
    else {
      word = mode === "standby" ? uiText("Standby") : uiText("Ready");
      why = snap && /^Ask your next/.test(snap.status && snap.status.why || "") ?
        uiText("Ask your next question - no wake word needed for a few seconds.") :
        uiText("Say “{phrase}”, then your question.", {phrase: PHRASE});
    }
    stateEl.textContent = word;
    whyEl.textContent = why;
    root.dataset.phase = p;
    if (p === "listening") startLive(); else stopLive();
    if (p !== shownPhase) {
      shownPhase = p;
      renderLog();
    }
  }
  function startLive() {        // the orb and the dots move with the microphone's loudness while Nana listens
    if (liveTimer) return;
    liveTimer = setInterval(function () {
      fetch(SERVICE + "/live", {credentials: "omit"}).then(function (r) { return r.json(); }).then(function (l) {
        root.style.setProperty("--nana-lvl", Math.min(1, (l.mic || 0) * 4).toFixed(3));
      }).catch(function () { /* the stream says if Nana went away */ });
    }, 140);
  }
  function stopLive() {
    if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }
    root.style.setProperty("--nana-lvl", "0");
  }
  function setMode(next, animate) {
    var before = mode;
    mode = next;
    root.dataset.mode = next;
    var open = next === "on";
    card.inert = !open;
    pill.setAttribute("aria-pressed", next === "off" ? "false" : "true");
    pill.setAttribute("aria-label", next === "off" ? uiText("Turn Nana on") : uiText("Open Nana"));
    pill.title = pill.getAttribute("aria-label");
    powerBtn.setAttribute("aria-pressed", next === "off" ? "false" : "true");
    if (animate && before !== next) {
      var kind = next === "off" ? "nana-power-down" : before === "off" ? "nana-power-up" : "nana-fold";
      root.classList.remove("nana-power-down", "nana-power-up", "nana-fold");
      void root.offsetWidth;               // restart the animation
      root.classList.add(kind);
      setTimeout(function () { root.classList.remove(kind); }, 1100);
    }
    if (open && before !== "on") setTimeout(function () { logEl.scrollTop = logEl.scrollHeight; }, 50);
    paint();
  }

  // --- the conversation: each interaction is what was asked, then Nana's cards --------------------------------------
  function last() { return entries.length ? entries[entries.length - 1] : null; }
  function cardKey(a) {     // a waiting card counts down every second: it is the same card while its id is the same
    return WAITING[a.outcome] ? a.outcome + ":" + a.id : JSON.stringify(a);
  }
  function find(turn) {
    for (var i = entries.length - 1; i >= 0; i--) if (entries[i].turn === turn) return entries[i];
    return null;
  }
  function handle(s) {
    snap = s;
    setOffline(false);
    if (s.clinic && s.clinic.device_id) device = s.clinic.device_id;
    if (since === null || (s.turns || 0) < since) from(s);      // Nana started again: its count starts again
    var e = null, changed = false, woke = false;
    if (s.turn > since && !find(s.turn)) {
      entries.push({turn: s.turn, heard: null, cards: []});
      changed = woke = true;
    }
    // what an answer belongs to: the interaction in progress, else the one whose question is still known
    var t = s.turn || (s.asked && s.asked.turn) || 0;
    e = t ? find(t) : null;
    if (!e && !t && last() && last().turn > since && !last().cards.length) e = last();   // ended before it was heard
    if (e && s.asked && s.asked.turn === e.turn && e.heard !== s.asked.text) {
      e.heard = s.asked.text;
      changed = true;
    }
    var a = s.answer;
    if (a && a.outcome === "cancelled" && entries.length > 1 && last().turn === s.turn && !last().heard) {
      e = entries[entries.length - 2];          // a new wake cancelled the one before: its card goes there
    }
    if (a && e) {
      var key = cardKey(a);
      var prev = e.cards.length ? e.cards[e.cards.length - 1] : null;
      if (!prev || prev.key !== key) {
        var copy = JSON.parse(JSON.stringify(a));
        delete copy.left;
        e.cards.push({key: key, a: copy});
        if (!e.heard && a.heard) e.heard = a.heard;
        changed = woke = true;
      }
    }
    if (entries.length > MAX) entries = entries.slice(entries.length - MAX);
    if (woke && mode === "standby") { setMode("on", true); keep(PREF, "on"); }
    if (changed) {
      keep(LOG, entries, true);
      renderLog();
    }
    paint();
  }
  function live(a) {        // a card still waits for a click only while Nana still shows it
    return !!(WAITING[a.outcome] && snap && snap.answer && snap.answer.id === a.id && mode !== "off");
  }
  function renderLog() {
    var fix = document.getElementById("nana-fix"), typed = fix ? fix.value : null, typedFor = fix ? fix.dataset.id : null,
      focused = fix && document.activeElement === fix;
    logEl.replaceChildren();
    var p = phase(), first = drawn === null;
    if (first) drawn = {};
    function fresh(node, key) {   // the history from the last page comes back as it was, still
      if (!drawn[key] && !first) node.classList.add("nana-new");
      drawn[key] = 1;
      return node;
    }
    entries.forEach(function (e, i) {
      var turn = mk("div", "", "nana-turn"), newest = i === entries.length - 1 && snap && e.turn && e.turn === snap.turn;
      if (e.heard) {
        var you = mk("div", "", "nana-msg nana-you");
        you.append(mk("span", uiText("You"), "nana-who"), mk("p", "“" + e.heard + "”"));
        turn.append(fresh(you, e.turn + ":you"));
      } else if (newest && p === "listening") {
        turn.append(fresh(dots(uiText("Listening...")), e.turn + ":listening"));
      }
      e.cards.forEach(function (c) { turn.append(fresh(answer(c.a, live(c.a)), e.turn + ":" + c.key)); });
      var waits = e.cards.length && WAITING[e.cards[e.cards.length - 1].a.outcome];
      if (newest && p === "thinking" && e.heard && (!e.cards.length || waits)) {
        turn.append(fresh(dots(uiText("Thinking...")), e.turn + ":thinking:" + e.cards.length));
      }
      logEl.append(turn);
    });
    if (!entries.length) {
      logEl.append(mk("p", uiText("Nothing asked yet."), "nana-empty"));
    }
    var again = document.getElementById("nana-fix");
    if (again && typed !== null && again.dataset.id === typedFor) {
      again.value = typed;
      if (focused) again.focus();
    }
    logEl.scrollTop = logEl.scrollHeight;
  }
  function dots(label) {
    var d = mk("div", "", "nana-msg nana-bot nana-wait");
    var b = mk("span", "", "nana-typing");
    b.setAttribute("aria-hidden", "true");
    b.append(mk("i"), mk("i"), mk("i"));
    d.append(b, mk("span", label, "nana-muted"));
    return d;
  }
  function decide(id, decision, area, text) {
    var note = area.querySelector(".nana-sent") || mk("p", "", "nana-muted nana-sent");
    area.append(note);
    area.querySelectorAll("button").forEach(function (b) { b.disabled = true; });
    note.textContent = uiText("Sending your choice...");
    var body = text === undefined ? {id: id, decision: decision} : {id: id, decision: decision, text: text};
    return post(SERVICE + "/confirm", body).then(function (r) {
      if (r.status === 204) {
        note.textContent = decision === "ask" ? uiText("Confirmed - asking.") :
          decision.indexOf("save:") === 0 ? uiText("Saving...") :
          decision.indexOf("open:") === 0 ? uiText("Opening the record.") : uiText("Discarded - nothing was done.");
        return;
      }
      return r.json().catch(function () { return {}; }).then(function (b) {
        note.textContent = REFUSED[b.result] || uiText("Nana did not take your choice - nothing was done.");
      });
    }).catch(function () {
      note.textContent = uiText("Your choice did not reach Nana - nothing was done.");
      area.querySelectorAll("button").forEach(function (b) { b.disabled = false; });
    });
  }
  function answer(a, waiting) {
    var b = mk("div", "", "nana-msg nana-bot");
    b.append(mk("span", "Nana", "nana-who"));
    if (a.outcome === "confirm") {
      b.append(mk("p", uiText("Did I hear you right?"), "nana-title"));
      if (!waiting) { b.append(mk("p", uiText("No longer waiting."), "nana-muted")); return b; }
      var label = mk("label", uiText("What to ask - correct it here if I misheard"), "nana-label");
      var fix = mk("input", "", "form-control form-control-sm nana-fix");
      label.htmlFor = "nana-fix";
      fix.id = "nana-fix"; fix.type = "text"; fix.maxLength = 300; fix.autocomplete = "off"; fix.value = a.heard || "";
      fix.dataset.id = a.id;
      b.append(label, fix, row([
        button(uiText("Yes, ask this"), "primary", function () { decide(a.id, "ask", b, fix.value); }),
        button(uiText("No, discard"), "outline-light", function () { decide(a.id, "discard", b); })
      ]));
      return b;
    }
    var title = a.title ? (TITLES[a.title] || a.title) : "";
    if (title) b.append(mk("p", title, "nana-title"));
    if (a.corrected) b.append(mk("p", uiText("You corrected it to: {words}", {words: a.corrected}), "nana-muted"));
    var d = a.device;
    (a.citations || []).forEach(function (c) {
      var q = mk("blockquote", c.passage, "nana-quote");
      if (c.language) q.lang = c.language;
      b.append(q, mk("p", (d ? d.make + " " + d.model + " · " : "") +
                          uiText("{title}, page {page}", {title: c.title, page: c.page}), "nana-muted"));
      if (Number.isInteger(c.source_id) && Number.isInteger(c.page)) {
        b.append(row([go(uiText("Open page {page}", {page: c.page}),
                         "/guides/sources/" + c.source_id + "?page=" + c.page + "#page-" + c.page)]));
      }
    });
    (a.warnings || []).forEach(function (w) {
      b.append(mk("p", uiText("Warning: {words} (page {page})", {words: w.text, page: w.page}), "nana-warn"));
    });
    if (a.outcome === "note_confirm") {
      b.append(mk("blockquote", a.text, "nana-quote"));
      if (waiting) {
        var acts = (a.choices || []).map(function (c) {
          return button(uiText("Save to {name}", {name: c.name}), "primary", function () { decide(a.id, "save:" + c.cf, b); },
                        uiText("Save to {name}", {name: c.name}) + " · " + c.cf);
        });
        acts.push(button(uiText("Discard"), "outline-light", function () { decide(a.id, "discard", b); }));
        b.append(row(acts));
      } else {
        b.append(mk("p", uiText("No longer waiting."), "nana-muted"));
      }
    }
    if (a.message && !PATIENTS[a.outcome]) b.append(mk("p", a.message));
    if (a.outcome === "note_saved" && a.patient) {
      b.append(row([go(uiText("Open record"), record(a.patient.cf), uiText("Open {name}'s record", {name: a.patient.name}))]));
    }
    if (PATIENTS[a.outcome]) b.append(patients(a, waiting));
    if (a.escalation) b.append(mk("p", uiText("What to do: {words}", {words: a.escalation}), "nana-muted"));
    if (a.devices) {
      b.append(mk("p", uiText("Say which one, for example: {list}", {list: a.devices.map(function (x) {
        return x.make + " " + x.model + " (" + x.room + ")";
      }).join("; ")})));
    }
    if (b.childNodes.length === 1 && a.say) b.append(mk("p", a.say));
    return b;
  }
  function patients(a, waiting) {
    var area = mk("div", "", "nana-patients"), list = mk("ul", "", "nana-plist");
    if (a.message) area.append(mk("p", a.message));
    (a.choices || []).forEach(function (c) {
      var li = mk("li", ""), head = mk("p", "", "nana-pname");
      head.append(mk("strong", c.name), mk("span", " · " + c.cf, "nana-cf"));
      li.append(head);
      if (a.outcome === "patient_next") {
        var n = c.next;
        li.append(mk("p", n ? uiText("{weekday} {date}, {time} with {dentist} ({minutes} min)", {
          weekday: DAYS[n.weekday] || n.weekday, date: n.date, time: n.time, dentist: n.dentist, minutes: n.minutes
        }) : uiText("No appointment booked"), "nana-when"));
      }
      if (a.outcome === "edit_confirm" || a.outcome === "edit_saved") {
        var change = mk("dl", "", "nana-change"), none = uiText("no phone number");
        if (a.outcome === "edit_confirm") {
          change.append(mk("dt", uiText("Now")), mk("dd", c.before || none), mk("dt", uiText("After saving")), mk("dd", a.value));
        } else if (a.before !== undefined) {
          change.append(mk("dt", uiText("Before")), mk("dd", a.before || none), mk("dt", uiText("Now")), mk("dd", a.value));
        }
        li.append(change);
      }
      var acts = [];
      if (a.outcome === "edit_confirm") {
        if (waiting) {
          acts.push(button(uiText("Save to {name}", {name: c.name}), "primary", function () { decide(a.id, "save:" + c.cf, area); },
                           uiText("Save to {name}", {name: c.name}) + " · " + c.cf));
        }
      } else {
        var pick = a.outcome === "patient_pick" && waiting ? function () { decide(a.id, "open:" + c.cf, area); } : null;
        acts.push(go(uiText("Open record"), record(c.cf), uiText("Open {name}'s record", {name: c.name}), pick));
      }
      li.append(row(acts));
      list.append(li);
    });
    area.append(list);
    if (waiting && (a.outcome === "edit_confirm" || a.outcome === "patient_pick")) {
      area.append(row([button(uiText("Discard"), "outline-light", function () { decide(a.id, "discard", area); })]));
    } else if (!waiting && WAITING[a.outcome]) {
      area.append(mk("p", uiText("No longer waiting."), "nana-muted"));
    }
    if (a.outcome === "edit_saved" && a.undo) area.append(mk("p", a.undo, "nana-muted"));
    return area;
  }

  // --- the controls -------------------------------------------------------------------------------------------------
  pill.addEventListener("click", function () {
    if (mode === "off") turnOn("on");
    else { keep(PREF, "on"); setMode("on", true); }
  });
  powerBtn.addEventListener("click", function () { if (mode === "off") turnOn("on"); else turnOff(); });
  standbyBtn.addEventListener("click", function () {
    keep(PREF, "standby");
    setMode("standby", true);
    pill.focus();
  });
  card.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { keep(PREF, "standby"); setMode("standby", true); pill.focus(); }
  });

  // --- start: as this person left it in this browser -----------------------------------------------------------------
  renderLog();
  setMode("off", false);
  var pref = load(PREF, false);
  if (pref === "on" || pref === "standby") {
    status().then(function (s) {
      device = s.clinic && s.clinic.device_id;
      if (s.access && s.access.username === USER && !s.paused) {     // already acting for this dentist: no new hand-over
        connect();
        setMode(pref, false);
        handle(s);
      } else {
        turnOn(pref);
      }
    }, function () { say(NOT_RUNNING); });
  }
})();
