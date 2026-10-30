// P30 sign-in core. Loaded on the login screen only.
//
// The Jarvis orb (jarvis/web.py: particles on rings around a glowing core),
// recoloured for the portal and given a face. It hears that a key was pressed,
// never what was typed: the drawing reads no field. Decoration only: the
// canvases and the caption are aria-hidden and the alert above the form stays
// the message.
//
// The form is sent in the background, as it is and to where it always went.
// The server's own redirect is the only proof of a sign-in: the core turns
// green, then the portal opens. Any other answer is the login page again, whose
// alert and state are taken over in place. Without fetch the form posts as it
// always did.
(function () {
  "use strict";
  var host = document.querySelector("[data-login-core]");
  var form = document.querySelector("[data-login-form]");
  if (!host || !form || !host.querySelector("canvas")) return;

  var REDUCE = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var MOTION = REDUCE ? 0.15 : 1;
  var INK = "rgba(21, 48, 42, 0.92)";

  // per mood: c on the dark panel, d on the white card, glow, the core's centre
  var MOODS = {
    idle:    { c: [140, 222, 182], d: [47, 133, 98],  g: 0.55, core: [230, 247, 236] },
    cf:      { c: [166, 238, 204], d: [34, 142, 100], g: 0.8,  core: [238, 252, 242] },
    pin:     { c: [128, 212, 196], d: [30, 122, 112], g: 0.72, core: [230, 248, 244] },
    check:   { c: [196, 242, 218], d: [38, 152, 112], g: 0.95, core: [246, 255, 249] },
    wrong:   { c: [255, 86, 52],   d: [214, 54, 30],  g: 1,    core: [255, 222, 196] },
    locked:  { c: [196, 70, 56],   d: [168, 60, 50],  g: 0.42, core: [232, 196, 182] },
    ok:      { c: [86, 226, 140],  d: [26, 150, 80],  g: 1,    core: [222, 255, 230] }
  };
  MOODS.expired = MOODS.locked;
  var ENERGY = { idle: 0.12, cf: 0.3, pin: 0.22, check: 0.7, wrong: 0.9, locked: 0.05, expired: 0.05, ok: 0.6 };

  function lerp(a, b, k) { return a + (b - a) * k; }
  function rgba(c, a) {
    return "rgba(" + Math.round(c[0]) + "," + Math.round(c[1]) + "," + Math.round(c[2]) + "," +
      Math.max(0, Math.min(1, a)).toFixed(3) + ")";
  }

  function Core(canvas, opts) {
    this.cv = canvas;
    this.ctx = canvas.getContext("2d");
    this.small = !!opts.small;
    this.light = !!opts.light;   // on the white card: darker colours, painted rather than added as light
    this.parts = [];
    var n = this.small ? 420 : 620;
    for (var i = 0; i < n; i++) {
      var ring = Math.random() < 0.6 ? 1 : (Math.random() < 0.55 ? 0.66 : 0.38);
      this.parts.push({ a: Math.random() * 6.283, r: ring * (0.88 + Math.random() * 0.24),
        s: (Math.random() - 0.5) * (2.2 - ring), l: 1 + Math.random() * 7, w: Math.random() });
    }
    var m = MOODS.idle;
    this.mood = "idle"; this.face = "open";
    this.col = m.c.slice(); this.dark = m.d.slice(); this.coreCol = m.core.slice(); this.glow = m.g;
    this.level = 0; this.target = ENERGY.idle; this.kick = 0; this.shake = 0; this.nod = 0;
    this.flameRate = 0; this.wrongAt = 0; this.size = 0;
    this.ripples = []; this.flames = [];
    this.look = { x: 0, y: 0, tx: 0, ty: 0 };
    this.blinkAt = 2500; this.blinkT = -1;
  }

  Core.prototype.set = function (mood, face, now) {
    this.mood = mood; this.face = face; this.target = ENERGY[mood];
    this.flameRate = 0;
    if (mood === "wrong") { this.shake = 1; this.flameRate = 1; this.wrongAt = now; }
    if (mood === "locked" || mood === "expired") { this.shake = 0.4; this.flameRate = 0.06; }
    if (mood === "ok") {
      for (var k = 0; k < 3; k++) this.ripples.push({ born: now + k * 140, dur: 1100, from: 0.3, to: 1.35, width: 3.2 });
    }
  };

  // a key went down: a ripple out, or in for a delete, and a small nod
  Core.prototype.key = function (deleting, now) {
    this.kick = Math.min(1, this.kick + (this.mood === "pin" ? 0.22 : 0.32));
    this.nod = 1;
    this.ripples.push(deleting ? { born: now, dur: 650, from: 1.05, to: 0.32, width: 1.6 }
                               : { born: now, dur: 750, from: 0.3, to: 1.08, width: 1.6 });
    if (this.ripples.length > 14) this.ripples.shift();
  };

  Core.prototype.lookAt = function (x, y) { this.look.tx = x; this.look.ty = y; };

  Core.prototype.fit = function () {
    var w = this.cv.clientWidth;
    if (!w || w === this.size) return w;
    var d = window.devicePixelRatio || 1;
    this.cv.width = w * d; this.cv.height = w * d;
    this.ctx.setTransform(d, 0, 0, d, 0, 0);
    this.size = w;
    return w;
  };

  Core.prototype.frame = function (t) {
    if (!this.cv.offsetParent) return;   // hidden at this width
    var w = this.fit();
    if (!w) return;
    var ctx = this.ctx, m = MOODS[this.mood], i;
    for (i = 0; i < 3; i++) {
      this.col[i] = lerp(this.col[i], m.c[i], 0.07);
      this.dark[i] = lerp(this.dark[i], m.d[i], 0.07);
      this.coreCol[i] = lerp(this.coreCol[i], m.core[i], 0.07);
    }
    this.glow = lerp(this.glow, m.g, 0.07);
    this.level += (this.target - this.level) * 0.1;
    this.kick *= 0.9; this.nod *= 0.88;
    if (this.mood === "wrong") {
      var since = t - this.wrongAt;
      if (since > 380 && since < 420) this.shake = Math.max(this.shake, 0.8);   // shakes its head twice
      if (since > 1900) this.flameRate = Math.max(0.08, this.flameRate * 0.985);  // flames settle to embers
    }
    var amp = Math.min(1.3, this.level + this.kick), c = w / 2, R = w * 0.36, glow = this.glow;
    var col = this.light ? this.dark : this.col;
    var sx = REDUCE ? 0 : Math.sin(t / 24) * this.shake * R * 0.07;
    this.shake *= 0.94;
    var speed = (this.mood === "check" ? 3.2 : (this.mood === "locked" || this.mood === "expired") ? 0.3 : 1) * MOTION;
    var squeeze = this.mood === "pin" ? 0.93 : this.mood === "ok" ? 1.02 : 1;
    var thin = this.small ? 0.85 : 1;

    // fade the last frame, keeping the canvas see-through
    ctx.globalCompositeOperation = "destination-out";
    ctx.fillStyle = "rgba(0, 0, 0, 0.28)";
    ctx.fillRect(0, 0, w, w);
    ctx.globalCompositeOperation = this.light ? "source-over" : "lighter";

    for (i = 0; i < this.parts.length; i++) {
      var p = this.parts[i];
      p.a += p.s * 0.003 * speed * (1 + amp * 5);
      var jit = this.mood === "wrong" ? (Math.random() - 0.5) * 0.05 * MOTION : 0;
      var rr = R * squeeze * (p.r + jit) * (1 + amp * 0.3 * Math.sin(t / 170 + p.w * 14) * MOTION);
      var x = c + sx + Math.cos(p.a) * rr, y = c + Math.sin(p.a) * rr, len = p.l * (1 + amp * 3) * thin;
      ctx.strokeStyle = rgba(col, (0.2 + 0.65 * p.w) * glow);
      ctx.lineWidth = (0.8 + amp * 1.6 * p.w) * thin;
      ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x - Math.sin(p.a) * len, y + Math.cos(p.a) * len); ctx.stroke();
    }
    for (var k = 0; k < 3; k++) {
      var s0 = (t / (800 + k * 350)) * speed + k * 2.1;
      var arc = this.light ? col : [Math.min(255, col[0] + 50), Math.min(255, col[1] + 30), Math.min(255, col[2] + 30)];
      ctx.strokeStyle = rgba(arc, 0.22 * glow + amp * 0.32);
      ctx.lineWidth = 1.4 * thin;
      ctx.beginPath(); ctx.arc(c + sx, c, R * (0.46 + k * 0.13) * (1 + amp * 0.16), s0, s0 + 1.5 + amp * 2.2); ctx.stroke();
    }
    for (i = this.ripples.length - 1; i >= 0; i--) {
      var rp = this.ripples[i], pr = (t - rp.born) / rp.dur;
      if (pr < 0) continue;
      if (pr >= 1) { this.ripples.splice(i, 1); continue; }
      var e = 1 - Math.pow(1 - pr, 3);
      ctx.strokeStyle = rgba(col, (1 - pr) * 0.55 * glow);
      ctx.lineWidth = rp.width * thin;
      ctx.beginPath(); ctx.arc(c + sx, c, R * lerp(rp.from, rp.to, e), 0, 6.283); ctx.stroke();
    }
    this.burn(t, c, R, sx, w);

    var pulse = this.mood === "check" ? Math.sin(t / 110) * 0.03 * MOTION : 0;
    var core = R * (0.24 + amp * 0.05 + pulse);
    var grad = ctx.createRadialGradient(c + sx, c, 0, c + sx, c, core * 2.4);
    grad.addColorStop(0, this.light ? rgba(col, 0.26 * glow + amp * 0.1) : rgba(this.coreCol, Math.min(1, 0.55 * glow + amp * 0.4)));
    grad.addColorStop(0.42, rgba(col, this.light ? 0.12 * glow + amp * 0.06 : 0.38 * glow + amp * 0.18));
    grad.addColorStop(1, "rgba(0, 0, 0, 0)");
    ctx.fillStyle = grad;
    ctx.beginPath(); ctx.arc(c + sx, c, core * 2.4, 0, 6.283); ctx.fill();

    // the core itself, solid, and its face on top
    ctx.globalCompositeOperation = "source-over";
    var disc = core * 0.98, cx = c + sx;
    var dg = ctx.createRadialGradient(cx - disc * 0.25, c - disc * 0.3, disc * 0.1, cx, c, disc);
    dg.addColorStop(0, rgba([Math.min(255, this.coreCol[0] + 14), Math.min(255, this.coreCol[1] + 8), Math.min(255, this.coreCol[2] + 8)], 1));
    dg.addColorStop(1, rgba([lerp(this.coreCol[0], col[0], 0.45), lerp(this.coreCol[1], col[1], 0.45), lerp(this.coreCol[2], col[2], 0.45)], 1));
    ctx.fillStyle = dg;
    ctx.beginPath(); ctx.arc(cx, c, disc, 0, 6.283); ctx.fill();
    if (this.light) { ctx.strokeStyle = rgba(col, 0.45); ctx.lineWidth = 1.5; ctx.stroke(); }
    this.drawFace(cx, c, disc, t);
  };

  // flames rise off the core after a wrong PIN, settle to embers, and stop when typing starts again
  Core.prototype.burn = function (t, c, R, sx, w) {
    var ctx = this.ctx, i, scale = w / 420;
    var rate = this.flameRate * (REDUCE ? 0.25 : 1) * (this.small ? 2 : 4.5);
    var born = Math.floor(rate) + (Math.random() < rate % 1 ? 1 : 0);
    for (i = 0; i < born; i++) {
      var a = -1.5708 + (Math.random() - 0.5) * 3.6, r0 = R * (0.22 + Math.random() * 0.14);
      this.flames.push({ x: Math.cos(a) * r0, y: Math.sin(a) * r0 * 0.9,
        vx: (Math.random() - 0.5) * 0.7 * scale, vy: -(1.1 + Math.random() * 1.8) * scale,
        life: 0, max: 38 + Math.random() * 34, size: R * (0.05 + Math.random() * 0.07), id: Math.random() * 9 });
    }
    for (i = this.flames.length - 1; i >= 0; i--) {
      var f = this.flames[i];
      f.life++;
      if (f.life > f.max) { this.flames.splice(i, 1); continue; }
      f.x += f.vx + Math.sin(t / 90 + f.id) * 0.35 * scale * MOTION;
      f.y += f.vy * MOTION;
      f.size *= 0.982;
      var q = 1 - f.life / f.max, fc = q > 0.75 ? [255, 196, 96] : q > 0.4 ? [255, 120, 36] : [206, 44, 22];
      var gx = c + sx + f.x, gy = c + f.y, fg = ctx.createRadialGradient(gx, gy, 0, gx, gy, f.size * 2.2);
      fg.addColorStop(0, rgba(fc, (this.light ? 0.6 : 0.42) * q + 0.08));
      fg.addColorStop(1, rgba(fc, 0));
      ctx.fillStyle = fg;
      ctx.beginPath(); ctx.arc(gx, gy, f.size * 2.2, 0, 6.283); ctx.fill();
    }
  };

  // open eyes that blink and follow the form; shut for the PIN; angry brows and a frown for a
  // wrong PIN; tired lines for a lockout; a smile for a sign-in
  Core.prototype.drawFace = function (cx, cy, r, t) {
    var ctx = this.ctx, L = this.look;
    L.x = lerp(L.x, L.tx, 0.12); L.y = lerp(L.y, L.ty, 0.12);
    if (t > this.blinkAt && this.blinkT < 0) this.blinkT = t;
    var blink = 1;
    if (this.blinkT >= 0) {
      var bp = (t - this.blinkT) / 160;
      if (bp < 1) blink = Math.abs(1 - bp * 2) * 0.9 + 0.1;
      else { this.blinkT = -1; this.blinkAt = t + 2600 + Math.random() * 3200; }
    }
    var ex = r * 0.36, ey = -r * 0.06 + this.nod * r * 0.05, ew = r * 0.15, eh = r * 0.22;
    var lx = L.x * r * 0.14, ly = L.y * r * 0.12;
    ctx.fillStyle = INK; ctx.strokeStyle = INK; ctx.lineCap = "round"; ctx.lineWidth = Math.max(1.4, r * 0.085);
    for (var side = -1; side <= 1; side += 2) {
      var x = cx + side * ex + lx, y = cy + ey + ly;
      ctx.beginPath();
      if (this.face === "open") { ctx.ellipse(x, y, ew * 0.5, Math.max(0.6, eh * 0.5 * blink), 0, 0, 6.283); ctx.fill(); }
      else if (this.face === "focus") { ctx.ellipse(x, y, ew * 0.42, eh * 0.3, 0, 0, 6.283); ctx.fill(); }
      else if (this.face === "closed") { ctx.arc(x, y - eh * 0.2, ew * 0.62, 0.18 * Math.PI, 0.82 * Math.PI); ctx.stroke(); }
      else if (this.face === "happy") { ctx.arc(x, y + eh * 0.25, ew * 0.62, 1.15 * Math.PI, 1.85 * Math.PI); ctx.stroke(); }
      else if (this.face === "tired") { ctx.moveTo(x - ew * 0.55, y + eh * 0.1); ctx.lineTo(x + ew * 0.55, y + eh * 0.1); ctx.stroke(); }
      else if (this.face === "angry") {
        ctx.ellipse(x, y + eh * 0.12, ew * 0.4, eh * 0.3, 0, 0, 6.283); ctx.fill();
        ctx.beginPath(); ctx.moveTo(x + side * ew * 0.95, y - eh * 0.95); ctx.lineTo(x - side * ew * 0.75, y - eh * 0.3); ctx.stroke();
      }
    }
    ctx.beginPath();
    if (this.face === "angry") { ctx.arc(cx + lx * 0.5, cy + r * 0.5, r * 0.17, 1.2 * Math.PI, 1.8 * Math.PI); ctx.stroke(); }
    else if (this.face === "happy") { ctx.arc(cx, cy + r * 0.16, r * 0.22, 0.18 * Math.PI, 0.82 * Math.PI); ctx.stroke(); }
    else if (this.face === "tired") { ctx.moveTo(cx - r * 0.12, cy + r * 0.42); ctx.lineTo(cx + r * 0.12, cy + r * 0.42); ctx.stroke(); }
  };

  var big = new Core(host.querySelector("canvas"), {});
  var cores = [big];
  var miniCanvas = document.querySelector("[data-login-core-mini]");
  var mini = miniCanvas ? new Core(miniCanvas, { small: true, light: true }) : null;
  if (mini) cores.push(mini);
  function loop(t) {
    for (var i = 0; i < cores.length; i++) cores[i].frame(t);
    window.requestAnimationFrame(loop);
  }
  window.requestAnimationFrame(loop);

  // ---- the form ----
  var cf = form.querySelector("#codice_fiscale");
  var pin = form.querySelector("#pin");
  var button = form.querySelector("button[type=submit]");
  var caption = document.querySelector("[data-core-caption]");
  var busy = false;

  function now() { return window.performance ? window.performance.now() : Date.now(); }
  function each(fn) { for (var i = 0; i < cores.length; i++) fn(cores[i]); }

  function mood(name, face, captionKey) {
    each(function (core) { core.set(name, face, now()); });
    if (caption) {
      var key = captionKey || name;
      var text = host.getAttribute("data-cap-" + key);
      if (text) caption.textContent = text;
      caption.setAttribute("data-mood", name);
    }
  }
  // the big core looks across at the form, the small one down at it
  function look(bx, by, mx, my) { big.lookAt(bx, by); if (mini) mini.lookAt(mx, my); }

  // the outcome lives on data-core-state (idle, check, wrong, locked, expired, ok); while it is
  // idle, the face follows the field in use
  function state(name) {
    host.setAttribute("data-core-state", name);
    if (name === "wrong") { mood("wrong", "angry"); look(0.6, 0, 0, 0.4); }
    else if (name === "locked" || name === "expired") { mood(name, "tired"); look(0.3, 0.2, 0, 0.3); }
    else if (name === "check") { mood("check", "focus"); look(0.2, -0.8, 0, -0.8); }
    else if (name === "ok") { mood("ok", "happy"); look(0, -0.1, 0, -0.1); }
    else settle();
  }
  function settle() {
    if (host.getAttribute("data-core-state") !== "idle") return;
    var active = document.activeElement;
    if (active === cf) { mood("cf", "open"); look(0.85, -0.1, 0, 0.9); }
    else if (active === pin && pin.type === "text") { mood("pin", "open", "away"); look(-0.9, -0.7, -0.9, -0.7); }
    else if (active === pin) { mood("pin", "closed"); look(0.85, 0.35, 0, 0.9); }
    else { mood("idle", "open"); look(0, 0, 0, 0); }
  }

  form.addEventListener("focusin", function () { settle(); });
  form.addEventListener("focusout", function () { window.setTimeout(settle, 0); });
  [cf, pin].forEach(function (field) {
    if (!field) return;
    field.addEventListener("input", function (event) {
      var current = host.getAttribute("data-core-state");
      if (current === "wrong" || current === "locked" || current === "expired") state("idle");
      var deleting = !!(event.inputType && event.inputType.indexOf("delete") === 0);
      each(function (core) { core.key(deleting, now()); });
    });
  });
  var toggle = form.querySelector("[data-pw-toggle]");
  // auth.js flips the PIN's type on the same click; read it once that has happened
  if (toggle) toggle.addEventListener("click", function () { window.setTimeout(settle, 0); });

  function landing() {
    // the server already chose this page from a local return address; keep to this site anyway
    var next = form.querySelector("input[name=next]");
    var target = next && next.value;
    return target && /^\/(?![\/\\])/.test(target) ? target : form.getAttribute("data-home") || "/";
  }
  function done() { busy = false; if (button) button.disabled = false; form.removeAttribute("aria-busy"); }

  function takeOver(html) {
    var doc = new window.DOMParser().parseFromString(html, "text/html");
    var answer = doc.querySelector("[data-login-core]");
    if (!answer) {
      // not the login page (an expired form, a server fault): show what the server sent,
      // exactly as a plain post would have
      document.open(); document.write(html); document.close();
      return;
    }
    var card = document.getElementById("patient-content");
    var old = card && card.querySelector(":scope > .alert");
    if (old) old.parentNode.removeChild(old);
    var alert = doc.querySelector("#patient-content > .alert");
    if (card && alert) card.insertBefore(document.importNode(alert, true), card.firstChild);
    var token = doc.querySelector("input[name=csrf_token]");
    var mine = form.querySelector("input[name=csrf_token]");
    if (token && mine) mine.value = token.value;
    pin.value = "";
    done();
    state(answer.getAttribute("data-core-state") || "wrong");
    pin.focus();
  }

  form.addEventListener("submit", function (event) {
    if (!window.fetch || !window.URLSearchParams || !window.FormData || !window.DOMParser) return;
    event.preventDefault();
    if (busy) return;
    busy = true;
    if (button) button.disabled = true;
    form.setAttribute("aria-busy", "true");
    // the last try's message goes with the new try; the answer brings its own
    var card = document.getElementById("patient-content");
    var stale = card && card.querySelector(":scope > .alert");
    if (stale) stale.parentNode.removeChild(stale);
    state("check");
    var started = now();
    // hold the checking state long enough to be seen, however fast the server is
    function after(fn) { window.setTimeout(fn, Math.max(0, 650 - (now() - started))); }
    window.fetch(form.action, {
      method: "POST",
      body: new window.URLSearchParams(new window.FormData(form)),
      credentials: "same-origin",
      redirect: "manual"
    }).then(function (response) {
      if (response.type === "opaqueredirect") {
        after(function () {
          state("ok");
          window.setTimeout(function () { window.location.assign(landing()); }, REDUCE ? 300 : 900);
        });
        return;
      }
      return response.text().then(function (html) { after(function () { takeOver(html); }); });
    }).catch(function () {
      // the request may never have left: post the form the plain way
      done();
      form.submit();
    });
  });

  // back from the portal through the browser's history: start over, waiting
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) { done(); host.setAttribute("data-core-state", "idle"); settle(); }
  });

  var opening = host.getAttribute("data-core-state");
  if (opening === "idle") settle(); else state(opening);
})();
