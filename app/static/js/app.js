// first-party app JS - plain event listeners, no build step, no framework.

// P29 E-001: what the scripts say, in the page's language. base.html carries the Italian of these sentences (empty
// in English); the English is the key, and {name} parts are filled in. list_tools.js and look.js use it too.
var UI_TEXT = (function () {
  var el = document.getElementById("ui-text");
  try { return el ? JSON.parse(el.textContent) : {}; } catch (e) { return {}; }
})();

function uiText(text, values) {
  var said = UI_TEXT[text] || text;
  Object.keys(values || {}).forEach(function (k) { said = said.split("{" + k + "}").join(values[k]); });
  return said;
}

function initToasts() {
  document.querySelectorAll(".toast:not([data-init])").forEach(function (el) {
    el.setAttribute("data-init", "1");
    new bootstrap.Toast(el).show();
  });
}

function renderStagedFiles(zone, files) {
  var scope = zone.closest("form") || zone;
  var container = scope.querySelector(".staged-files");
  var submitBtn = scope.querySelector("button[type=submit]");
  if (!container) return;

  container.textContent = "";
  files.forEach(function (file, index) {
    var chip = document.createElement("span");
    chip.className = "badge text-bg-light border d-inline-flex align-items-center gap-1 me-2 mb-2";

    var icon = document.createElement("i");
    icon.className = "bi bi-file-earmark";
    chip.appendChild(icon);

    var name = document.createElement("span");
    name.textContent = file.name;
    chip.appendChild(name);

    var remove = document.createElement("button");
    remove.type = "button";
    remove.className = "btn btn-sm btn-link text-secondary p-0 ms-1";
    remove.setAttribute("aria-label", uiText("Remove {name}", {name: file.name}));
    remove.innerHTML = '<i class="bi bi-x-lg"></i>';
    remove.addEventListener("click", function () {
      var input = zone.querySelector("input[type=file]");
      var dt = new DataTransfer();
      files.forEach(function (f, i) {
        if (i !== index) dt.items.add(f);
      });
      input.files = dt.files;
      input.dispatchEvent(new Event("change"));
    });
    chip.appendChild(remove);

    container.appendChild(chip);
  });

  if (submitBtn) submitBtn.disabled = files.length === 0;
}

function wireDropZones() {
  document.querySelectorAll(".upload-zone").forEach(function (zone) {
    var input = zone.querySelector("input[type=file]");
    if (!input) return;

    zone.addEventListener("dragover", function (event) {
      event.preventDefault();
      zone.classList.add("is-dragover");
    });
    zone.addEventListener("dragleave", function () {
      zone.classList.remove("is-dragover");
    });
    zone.addEventListener("drop", function (event) {
      event.preventDefault();
      zone.classList.remove("is-dragover");
      input.files = event.dataTransfer.files;
      input.dispatchEvent(new Event("change"));
    });
    input.addEventListener("change", function () {
      renderStagedFiles(zone, Array.from(input.files));
    });
  });
}

// the upload form posts over htmx and only swaps the toast container, so
// nothing clears the staged file afterwards - without this a second click
// re-uploads the same file and the sorter files it again as name_1.
function resetUploadForm(event) {
  if (!event.detail || !event.detail.successful) return;
  var form = event.target.closest ? event.target.closest("form") : null;
  if (!form) return;
  var zone = form.querySelector(".upload-zone");
  if (!zone) return;

  form.querySelector("input[type=file]").value = "";
  renderStagedFiles(zone, []);
}

document.addEventListener("DOMContentLoaded", function () {
  initToasts();
  wireDropZones();
});
document.body.addEventListener("htmx:afterSwap", initToasts);

// admin row swaps return a <tr>, so their toast arrives as rendered html on a
// trigger header instead of riding along in the response body
document.body.addEventListener("adminToast", function (event) {
  var container = document.getElementById("admin-toast-container");
  if (!container) return;
  container.insertAdjacentHTML("afterbegin", event.detail.value);
  initToasts();
});
document.body.addEventListener("htmx:afterRequest", resetUploadForm);

// show/hide password (UX-10). one input whose type flips - never a second
// input holding the same value, and aria-pressed so the state is announced
// rather than only drawn.
// each toggle drives the input beside it, so a form can have several (R-002).
(function () {
  document.querySelectorAll("[data-pw-toggle]").forEach(function (toggle) {
    var input = toggle.parentElement.querySelector("[data-pw-input]");
    if (!input) return;
    toggle.addEventListener("click", function () {
      var shown = input.type === "text";
      input.type = shown ? "password" : "text";
      toggle.setAttribute("aria-pressed", shown ? "false" : "true");
      // the page gives both names: aria-label to show, data-label-hide to hide
      if (!toggle.hasAttribute("data-label-show")) toggle.setAttribute("data-label-show", toggle.getAttribute("aria-label"));
      toggle.setAttribute("aria-label", toggle.getAttribute(shown ? "data-label-show" : "data-label-hide") || toggle.getAttribute("data-label-show"));
      var icon = toggle.querySelector("i");
      if (icon) icon.className = shown ? "bi bi-eye" : "bi bi-eye-slash";
    });
  });
})();

// segmented filter (phase 43). a .ds-segmented with data-filter-target names
// a list; each button's data-filter hides the rows whose data-when does not
// match. it filters rows the server already sent - nothing is fetched, so a
// role sees exactly what it saw before, just fewer of them at a time.
document.querySelectorAll(".ds-segmented[data-filter-target]").forEach(function (group) {
  var list = document.getElementById(group.dataset.filterTarget);
  if (!list) return;
  group.querySelectorAll("button[data-filter]").forEach(function (button) {
    button.addEventListener("click", function () {
      var want = button.dataset.filter;
      group.querySelectorAll("button[data-filter]").forEach(function (b) {
        b.setAttribute("aria-pressed", b === button ? "true" : "false");
      });
      list.querySelectorAll("[data-when]").forEach(function (row) {
        row.hidden = want !== "all" && row.dataset.when !== want;
      });
    });
  });
});

// record tabs (P29). without script every section is on the page; with it, one tab's sections at a time.
// arrow keys move between tabs, and a link to an #id inside a panel (e.g. #imaging) opens that panel.
document.querySelectorAll("[data-record-tabs]").forEach(function (list) {
  var tabs = Array.prototype.slice.call(list.querySelectorAll('[role="tab"]'));
  function select(tab, focus) {
    tabs.forEach(function (t) {
      var on = t === tab;
      t.setAttribute("aria-selected", on ? "true" : "false");
      t.tabIndex = on ? 0 : -1;
      document.getElementById(t.getAttribute("aria-controls")).hidden = !on;
    });
    if (focus) tab.focus();
  }
  tabs.forEach(function (tab, i) {
    tab.addEventListener("click", function () { select(tab, false); });
    tab.addEventListener("keydown", function (e) {
      var next = e.key === "ArrowRight" ? i + 1 : e.key === "ArrowLeft" ? i - 1 : null;
      if (e.key === "Home") next = 0;
      if (e.key === "End") next = tabs.length - 1;
      if (next === null) return;
      e.preventDefault();
      select(tabs[(next + tabs.length) % tabs.length], true);
    });
  });
  list.hidden = false;
  var target = window.location.hash ? document.getElementById(window.location.hash.slice(1)) : null;
  var panel = target ? target.closest('[role="tabpanel"]') : null;
  var start = panel ? list.querySelector('[aria-controls="' + panel.id + '"]') : tabs[0];
  select(start || tabs[0], false);
  if (target) target.scrollIntoView();
});

// P30 top bar clock: the server sends the clinic's time as minutes since midnight; this keeps it ticking.
// no zone is needed and nothing is stored. past midnight the date line is dropped rather than shown stale.
(function () {
  var clock = document.querySelector("[data-clock]");
  if (!clock) return;
  var time = clock.querySelector("[data-clock-time]");
  var day = clock.querySelector("[data-clock-day]");
  var start = parseInt(clock.getAttribute("data-minutes"), 10);
  var loaded = Date.now();
  if (isNaN(start) || !time) return;
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function tick() {
    var m = start + Math.floor((Date.now() - loaded) / 60000);
    if (m >= 1440 && day) day.hidden = true;
    m = m % 1440;
    time.textContent = pad(Math.floor(m / 60)) + ":" + pad(m % 60);
  }
  window.setInterval(tick, 15000);
})();

// P30 consent wording: every wording row is on the page; this folds each behind its row's Read out button.
(function () {
  Array.prototype.forEach.call(document.querySelectorAll("[data-read-out]"), function (button) {
    var row = document.getElementById(button.getAttribute("aria-controls"));
    if (!row) return;
    var word = button.firstChild;
    button.hidden = false;
    row.hidden = true;
    button.setAttribute("aria-expanded", "false");
    button.addEventListener("click", function () {
      var open = row.hidden;
      row.hidden = !open;
      button.setAttribute("aria-expanded", String(open));
      word.textContent = open ? uiText("Hide wording") : uiText("Read out");
    });
  });
})();

// P29 U-007 / R-021: a text field with a limit of 150 or more shows how much is used and says
// when the limit is reached; a textarea marked data-grow grows with what is typed. the server
// still cuts or refuses - this only says so before it happens
(function () {
  document.querySelectorAll("textarea[maxlength], input[maxlength]:not([type]), input[type=text][maxlength]").forEach(function (field) {
    var max = parseInt(field.getAttribute("maxlength"), 10);
    if (!max || max < 150 || field.name === "q" || field.hasAttribute("data-no-counter")) return;
    var counter = document.createElement("span");
    counter.className = "app-counter";
    field.insertAdjacentElement("afterend", counter);
    var said = document.createElement("span");
    said.className = "visually-hidden";
    said.setAttribute("role", "status");
    counter.insertAdjacentElement("afterend", said);
    function update() {
      var full = field.value.length >= max;
      var used = {n: field.value.length, max: max};
      counter.textContent = full ? uiText("{n} / {max} characters - limit reached, nothing more fits", used) : uiText("{n} / {max} characters", used);
      counter.classList.toggle("is-full", full);
      said.textContent = full ? uiText("Limit of {max} characters reached", used) : "";
      if (field.hasAttribute("data-grow")) {
        field.style.height = "auto";
        field.style.height = field.scrollHeight + "px";
      }
    }
    field.addEventListener("input", update);
    update();
  });
})();

// P29 NEW-02: the new-password checklist ticks as the rules are met. the server checks
// them again; this only shows progress
(function () {
  var list = document.querySelector("[data-pw-rules]");
  var input = document.getElementById("password");
  if (!list || !input) return;
  var tests = {
    upper: function (v) { return /[A-Z]/.test(v); },
    lower: function (v) { return /[a-z]/.test(v); },
    digit: function (v) { return /[0-9]/.test(v); },
    symbol: function (v) { return /[^A-Za-z0-9]/.test(v); },
    length: function (v) { return v.length >= 8; }
  };
  input.addEventListener("input", function () {
    list.querySelectorAll("[data-rule]").forEach(function (li) {
      var met = tests[li.getAttribute("data-rule")](input.value);
      li.classList.toggle("is-met", met);
      li.querySelector("i").className = met ? "bi bi-check-circle-fill" : "bi bi-circle";
      li.querySelector("[data-rule-state]").textContent = met ? uiText(" - done") : uiText(" - not yet");
    });
  });
})();

// P29 R-009: cancelling asks first, names the appointment, and waits five seconds unless
// "I am sure" is ticked. the wait only enables the button - time alone never cancels.
// without script the form posts as before.
(function () {
  var dialog = document.getElementById("cancel-dialog");
  if (!dialog || typeof dialog.showModal !== "function") return;
  var yes = dialog.querySelector("[data-cancel-yes]");
  var sure = dialog.querySelector("[data-cancel-sure]");
  var form = null, timer = null, left = 0;
  function label() { yes.textContent = left > 0 && !sure.checked ? uiText("Yes, cancel ({n})", {n: left}) : uiText("Yes, cancel"); }
  function arm() { yes.disabled = left > 0 && !sure.checked; label(); }
  document.querySelectorAll("form[data-confirm-cancel]").forEach(function (f) {
    f.addEventListener("submit", function (event) {
      if (f.dataset.confirmed) return;
      event.preventDefault();
      form = f;
      dialog.querySelector("#cancel-dialog-what").textContent = f.getAttribute("data-confirm-cancel");
      sure.checked = false;
      left = 5;
      arm();
      clearInterval(timer);
      timer = setInterval(function () { left -= 1; arm(); if (left <= 0) clearInterval(timer); }, 1000);
      dialog.showModal();
    });
  });
  sure.addEventListener("change", arm);
  dialog.querySelector("[data-cancel-keep]").addEventListener("click", function () { dialog.close(); });
  dialog.addEventListener("close", function () { clearInterval(timer); form = null; });
  yes.addEventListener("click", function () {
    if (!form || yes.disabled) return;
    var f = form;
    f.dataset.confirmed = "1";
    dialog.close();
    HTMLFormElement.prototype.submit.call(f);
  });
})();

// P29 R-010: a filter form whose values differ from what the page shows highlights its
// Show button, and drops it again when they match - nothing to do with hover.
(function () {
  document.querySelectorAll("[data-apply-button]").forEach(function (button) {
    var form = button.form;
    var hint = form.querySelector("[data-apply-hint]");
    var shown = new URLSearchParams(new FormData(form)).toString();
    function check() {
      var changed = new URLSearchParams(new FormData(form)).toString() !== shown;
      button.classList.toggle("btn-primary", changed);
      button.classList.toggle("btn-outline-secondary", !changed);
      if (hint) hint.hidden = !changed;
    }
    form.addEventListener("input", check);
    form.addEventListener("change", check);
  });
})();

// P30 billing: All / Outstanding / To reconcile / Drafts choose which group shows inside the one box.
(function () {
  var group = document.querySelector("[data-bill-show]");
  if (!group) return;
  var panels = Array.prototype.slice.call(document.querySelectorAll("[data-bill-panel]"));
  var buttons = Array.prototype.slice.call(group.querySelectorAll("[data-show]"));
  group.hidden = false;
  buttons.forEach(function (button) {
    button.addEventListener("click", function () {
      var show = button.getAttribute("data-show");
      buttons.forEach(function (b) { b.setAttribute("aria-pressed", String(b === button)); });
      panels.forEach(function (panel) { panel.hidden = !(show === "all" || panel.getAttribute("data-bill-panel") === show); });
    });
  });
})();

// P29 R-028: a form marked data-unsaved-guard with typed changes asks before a link inside the
// app takes the user away - in the app's own dialog. closing the tab, reloading or going back
// still gets the browser's own warning, the only one a page may show there.
(function () {
  var forms = document.querySelectorAll("[data-unsaved-guard]");
  var dialog = document.getElementById("leave-dialog");
  if (!forms.length) return;
  var dirty = false, target = null;
  forms.forEach(function (f) {
    if (f.hasAttribute("data-dirty-from-start")) dirty = true;
    f.addEventListener("input", function () { dirty = true; });
    f.addEventListener("submit", function () { dirty = false; });
  });
  window.addEventListener("beforeunload", function (e) { if (dirty) { e.preventDefault(); e.returnValue = ""; } });
  if (!dialog || typeof dialog.showModal !== "function") return;
  document.addEventListener("click", function (e) {
    var a = e.target.closest("a[href]");
    if (!dirty || !a || a.target === "_blank" || e.defaultPrevented || e.metaKey || e.ctrlKey || e.shiftKey) return;
    var url = new URL(a.href, location.href);
    if (url.origin !== location.origin || (url.pathname === location.pathname && url.hash)) return;
    e.preventDefault();
    target = url.href;
    dialog.showModal();
  });
  dialog.querySelector("[data-leave-stay]").addEventListener("click", function () { target = null; dialog.close(); });
  dialog.querySelector("[data-leave-go]").addEventListener("click", function () {
    var go = target;
    dirty = false;
    dialog.close();
    if (go) location.href = go;
  });
})();

// P29 U-009: a reason list's Other needs the words typed under it
document.querySelectorAll("[data-reason-pick]").forEach(function (pick) {
  var words = document.getElementById(pick.getAttribute("data-reason-pick"));
  if (!words) return;
  function sync() { words.required = pick.value === "other"; }
  pick.addEventListener("change", sync);
  sync();
});

// P29 U-021: the lookup results by keyboard. down from the box goes into the results, up and down move between
// patients, Enter opens one, Esc closes them and goes back to the box.
(function () {
  var box = document.querySelector(".app-search input[type=search]");
  var results = document.getElementById("app-search-results");
  if (!box || !results) return;
  function links() { return Array.prototype.slice.call(results.querySelectorAll("a[href]")); }
  box.addEventListener("keydown", function (e) {
    if (!links().length) return;
    if (e.key === "ArrowDown") { e.preventDefault(); links()[0].focus(); }
    else if (e.key === "Escape") { e.preventDefault(); results.innerHTML = ""; }
  });
  results.addEventListener("keydown", function (e) {
    var all = links(), i = all.indexOf(document.activeElement);
    if (i < 0) return;
    if (e.key === "ArrowDown") { e.preventDefault(); (all[i + 1] || all[i]).focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); (i === 0 ? box : all[i - 1]).focus(); }
    else if (e.key === "Escape") { e.preventDefault(); results.innerHTML = ""; box.focus(); }
  });
})();

// P29 R-031: a file preview that cannot be drawn says so in words instead of an empty grey frame
document.querySelectorAll("img.file-view, img.file-thumb, img.import-preview").forEach(function (img) {
  function fail() {
    var box = document.createElement("span");
    box.className = "file-noview";
    box.setAttribute("role", "img");
    box.setAttribute("aria-label", uiText("No preview: this image could not be shown"));
    box.textContent = img.classList.contains("file-thumb") ? uiText("No preview") : uiText("This image could not be shown here. Download the original to open it.");
    img.replaceWith(box);
  }
  if (img.complete && img.naturalWidth === 0 && img.getAttribute("src")) fail();
  else img.addEventListener("error", fail);
});

// P29 U-008: the result popup closes with OK or Esc and leaves the page as it is - it is not modal, nothing behind it
// is locked. when the page put the cursor nowhere, the cursor starts on OK so Enter closes it
(function () {
  var box = document.querySelector("[data-ux=result]");
  if (!box) return;
  var ok = box.querySelector("[data-result-ok]");
  var main = document.getElementById("main");
  function close() {
    var inside = box.contains(document.activeElement);
    box.hidden = true;
    if (inside && main) main.focus({ preventScroll: true });
  }
  ok.addEventListener("click", close);
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape" || box.hidden) return;
    var at = document.activeElement;
    if (box.contains(at) || at === document.body || at === main) close();
  });
  var auto = Array.prototype.some.call(document.querySelectorAll("[autofocus]"), function (el) { return !el.closest("dialog"); });
  if (!auto && document.activeElement === document.body) ok.focus();
})();

// P29 U-005: the menu opens with the page you are on in view. in a short window, a zoomed one or the phone drawer the
// menu is taller than the screen, and the current page can sit below the fold or under the pinned account foot
(function () {
  var side = document.getElementById("app-side");
  if (!side) return;
  function reveal() {
    var link = side.querySelector(".app-side-link.active");
    var foot = side.querySelector(".app-side-foot");
    if (!link) return;
    var top = side.getBoundingClientRect().top, at = link.getBoundingClientRect();
    var bottom = foot ? foot.getBoundingClientRect().top : side.getBoundingClientRect().bottom;
    if (at.top >= top && at.bottom <= bottom) return;
    side.scrollTop += at.top - top - (bottom - top - at.height) / 2;
  }
  reveal();
  side.addEventListener("show.bs.offcanvas", reveal);
})();

// P29 U-020: a save while the clinic app is not answering never leaves a white page. before a form posts, the page asks
// the server if it is there (a read, nothing is sent); if not, the page stays as it is with what was typed, says that
// nothing was sent, and offers Try again - which checks again and then posts once. htmx requests that cannot reach the
// server say the same. nothing is ever resent on its own
(function () {
  function reachable() {
    var stop = window.AbortController ? new AbortController() : null;
    var timer = stop ? setTimeout(function () { stop.abort(); }, 5000) : null;
    return fetch("/api/session", { cache: "no-store", credentials: "same-origin", redirect: "manual",
                                   signal: stop ? stop.signal : undefined })
      .then(function () { return true; }, function () { return false; })
      .then(function (ok) { if (timer) clearTimeout(timer); return ok; });
  }

  function say(retry) {
    var old = document.querySelector("[data-offline]");
    if (old) old.remove();
    var box = document.createElement("section");
    box.className = "app-result";
    box.setAttribute("data-offline", "");
    box.setAttribute("role", "alert");
    var line = document.createElement("div");
    line.className = "alert alert-danger app-flash";
    line.textContent = uiText("The clinic app is not answering. Nothing was sent, and what you typed is still here.");
    box.appendChild(line);
    var acts = document.createElement("div");
    acts.className = "app-result-actions gap-2";
    if (retry) {
      var again = document.createElement("button");
      again.type = "button";
      again.className = "btn btn-primary btn-sm";
      again.setAttribute("data-offline-retry", "");
      again.textContent = uiText("Try again");
      again.addEventListener("click", function () { box.remove(); retry(); });
      acts.appendChild(again);
    }
    var ok = document.createElement("button");
    ok.type = "button";
    ok.className = "btn btn-outline-secondary btn-sm";
    ok.textContent = uiText("OK");
    ok.addEventListener("click", function () { box.remove(); });
    acts.appendChild(ok);
    box.appendChild(acts);
    document.body.appendChild(box);
    (retry ? again : ok).focus();
  }

  function post(form, submitter) {
    reachable().then(function (ok) {
      if (!ok) { say(function () { post(form, submitter); }); return; }
      form.dataset.reached = "1";
      if (form.requestSubmit) form.requestSubmit(submitter || undefined); else form.submit();
    });
  }

  // capture: this runs before the page's own submit handlers, which then see the one real submit
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if ((form.getAttribute("method") || "get").toLowerCase() !== "post" || form.hasAttribute("hx-post")) return;
    if (form.dataset.reached === "1") { delete form.dataset.reached; return; }
    event.preventDefault();
    event.stopImmediatePropagation();
    post(form, event.submitter);
  }, true);

  document.body.addEventListener("htmx:sendError", function () { say(null); });
})();
