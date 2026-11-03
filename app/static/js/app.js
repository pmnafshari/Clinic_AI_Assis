// first-party app JS - plain event listeners, no build step, no framework.

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
    remove.setAttribute("aria-label", "Remove " + file.name);
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
      var label = toggle.getAttribute("aria-label").replace(/^(Show|Hide) /, "");
      toggle.setAttribute("aria-label", (shown ? "Show " : "Hide ") + label);
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
      word.textContent = open ? "Hide wording" : "Read out";
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
  function label() { yes.textContent = left > 0 && !sure.checked ? "Yes, cancel (" + left + ")" : "Yes, cancel"; }
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
