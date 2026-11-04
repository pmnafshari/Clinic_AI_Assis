// P30 list tools: search, Filter (a list of choices you tick), Sort (the orders that fit the table) and sortable
// column headers, for any list that declares them. Loaded on every staff page.
//
// A list declares itself in markup, so the same code serves patients, invoices, stock, call-backs and requests:
//   <div data-list-tools data-for="LIST-ID" data-noun="patient|patients"
//        data-sorts='[["name","text",1,"Name (A-Z)"], ...]'      key, text|num, 1 up / -1 down, label
//        data-filters='[["phone","Phone",[["yes","Has a phone"], ...]], ...]'   key, group, [value, label]
//        data-search data-find="name cf">                         optional search box over those data-* keys
// LIST-ID is the tbody or ul whose direct children are the rows; each row carries one data-* value per key it
// can be sorted or filtered by. Every row stays in the page: this only hides and reorders them, and without the
// script the list is simply the whole list. Nothing is stored in the browser, and a row's text is never written
// back as markup - controls are built with createElement and textContent.
//
// P29 U-004: a list too long for one page adds data-server. Then the same controls ask the server instead - a choice
// reloads the page with it in the address (filter keys, sort, dir) - and the server sends back the rows, the counts
// (data-counts), what is chosen (data-chosen) and how many match (data-shown of data-total).
(function () {
  "use strict";

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function icon(name) {
    var i = el("i", "bi bi-" + name);
    i.setAttribute("aria-hidden", "true");
    return i;
  }

  function dropdown(label, iconName, menu) {
    var wrap = el("div", "dropdown");
    var button = el("button", "btn btn-outline-secondary btn-sm sx-tool-btn");
    button.type = "button";
    button.setAttribute("data-bs-toggle", "dropdown");
    button.setAttribute("data-bs-auto-close", "outside");
    button.setAttribute("aria-expanded", "false");
    button.appendChild(icon(iconName));
    var text = el("span", "sx-tool-label", label);
    button.appendChild(text);
    button.appendChild(icon("chevron-down"));
    wrap.appendChild(button);
    menu.classList.add("dropdown-menu", "sx-tool-menu");
    wrap.appendChild(menu);
    return { wrap: wrap, button: button, label: text };
  }

  function init(box) {
    var list = document.getElementById(box.getAttribute("data-for"));
    if (!list) return;
    var sorts = JSON.parse(box.getAttribute("data-sorts") || "[]");
    var filters = JSON.parse(box.getAttribute("data-filters") || "[]");
    var noun = (box.getAttribute("data-noun") || "row|rows").split("|");
    var find = (box.getAttribute("data-find") || "name").split(" ");
    var root = list.closest("table") || list;
    root = root.closest(".sx-box") || root;      // the scroll box, so chips and the count stay put while the rows scroll
    var rows = Array.prototype.slice.call(list.children);
    rows.forEach(function (row, i) { row._order = i; });

    var server = box.hasAttribute("data-server");
    var counts = JSON.parse(box.getAttribute("data-counts") || "{}");
    var picked = JSON.parse(box.getAttribute("data-chosen") || "{}");
    var ready = false;
    var state = { q: "", chosen: {}, sort: parseInt(box.getAttribute("data-sort-initial") || "0", 10) };
    filters.forEach(function (g) { state.chosen[g[0]] = server ? (picked[g[0]] || []).slice() : []; });

    // ---- the controls ----
    var searchInput = null;
    if (box.hasAttribute("data-search")) {
      var form = el("form", "sx-search");
      form.setAttribute("role", "search");
      var label = el("label", "visually-hidden", uiText("Search this list"));
      searchInput = el("input", "form-control form-control-sm");
      searchInput.type = "search";
      searchInput.autocomplete = "off";
      searchInput.placeholder = box.getAttribute("data-search") || uiText("Search");
      searchInput.id = list.id + "-search";
      label.setAttribute("for", searchInput.id);
      var go = el("button", "btn btn-primary btn-sm", uiText("Search"));
      go.type = "submit";
      form.appendChild(label);
      form.appendChild(searchInput);
      form.appendChild(go);
      form.addEventListener("submit", function (e) { e.preventDefault(); state.q = searchInput.value.trim().toLowerCase(); apply(); });
      searchInput.addEventListener("input", function () { state.q = searchInput.value.trim().toLowerCase(); apply(); });
      box.appendChild(form);
    }

    var filterLabel = null, filterCount = null, boxes = {};
    if (filters.length) {
      var panel = el("div");
      filters.forEach(function (g) {
        panel.appendChild(el("p", "sx-tool-k", g[1]));
        boxes[g[0]] = {};
        g[2].forEach(function (option) {
          var n = server ? ((counts[g[0]] || {})[option[0]] || 0)
            : rows.filter(function (r) { return r.getAttribute("data-" + g[0]) === option[0]; }).length;
          var row = el("label", "sx-opt");
          var check = el("input");
          check.type = "checkbox";
          check.value = option[0];
          check.checked = state.chosen[g[0]].indexOf(option[0]) >= 0;
          check.addEventListener("change", function () {
            var chosen = state.chosen[g[0]], at = chosen.indexOf(option[0]);
            if (check.checked && at < 0) chosen.push(option[0]);
            if (!check.checked && at >= 0) chosen.splice(at, 1);
            apply();
          });
          boxes[g[0]][option[0]] = check;
          row.appendChild(check);
          row.appendChild(el("span", null, option[1]));
          row.appendChild(el("span", "sx-opt-n", String(n)));
          panel.appendChild(row);
        });
      });
      var foot = el("div", "sx-tool-foot");
      var clear = el("button", "btn btn-outline-secondary btn-sm", uiText("Clear"));
      clear.type = "button";
      clear.addEventListener("click", clearAll);
      foot.appendChild(clear);
      panel.appendChild(foot);
      var f = dropdown(uiText("Filter"), "funnel", panel);
      filterLabel = f.label;
      filterCount = el("span", "sx-tool-count");
      filterCount.hidden = true;
      f.button.insertBefore(filterCount, f.button.lastChild);
      box.appendChild(f.wrap);
    }

    var menu = el("div");
    menu.setAttribute("role", "menu");
    menu.appendChild(el("p", "sx-tool-k", uiText("Sort by")));
    var items = sorts.map(function (s, i) {
      var item = el("button", "sx-menu-item");
      item.type = "button";
      item.setAttribute("role", "menuitemradio");
      item.appendChild(icon("check2"));
      item.appendChild(el("span", null, s[3]));
      item.addEventListener("click", function () { state.sort = i; apply(); });
      menu.appendChild(item);
      return item;
    });
    var sortMenu = dropdown("", "arrow-down-up", menu);
    sortMenu.button.setAttribute("aria-label", uiText("Sort"));
    box.appendChild(sortMenu.wrap);

    // column headers sort too: a click picks that column, a second click flips it
    var headers = Array.prototype.slice.call((list.closest("table") || document.createElement("table")).querySelectorAll("th[data-sort]"));
    headers.forEach(function (th) {
      var key = th.getAttribute("data-sort");
      var button = el("button", "sx-th");
      button.type = "button";
      button.title = uiText("Sort by this column");
      while (th.firstChild) button.appendChild(th.firstChild);
      var arrow = el("span", "sx-th-arrow", "↕");
      arrow.setAttribute("aria-hidden", "true");
      button.appendChild(arrow);
      th.appendChild(button);
      button.addEventListener("click", function () {
        var now = sorts[state.sort];
        var same = now && now[0] === key;
        var want = same ? -now[2] : null;
        var at = -1;
        sorts.forEach(function (s, i) { if (s[0] === key && (want === null ? at < 0 : s[2] === want)) at = i; });
        if (at >= 0) { state.sort = at; apply(); }
      });
    });

    // ---- what shows around the list ----
    var chips = el("div", "sx-chips");
    chips.hidden = true;
    root.parentNode.insertBefore(chips, root);
    var status = el("p", "sx-list-status");
    status.setAttribute("role", "status");
    root.parentNode.insertBefore(status, root.nextSibling);
    var empty = el("p", "sx-list-empty");
    empty.hidden = true;
    empty.appendChild(document.createTextNode(uiText("No {nouns} match. ", {nouns: noun[1]})));
    var emptyClear = el("button", "btn btn-link btn-sm p-0 align-baseline", uiText("Clear search and filters"));
    emptyClear.type = "button";
    emptyClear.addEventListener("click", clearAll);
    empty.appendChild(emptyClear);
    root.parentNode.insertBefore(empty, status);

    function labelOf(key, value) {
      for (var i = 0; i < filters.length; i++) {
        if (filters[i][0] !== key) continue;
        for (var j = 0; j < filters[i][2].length; j++) if (filters[i][2][j][0] === value) return filters[i][1] + ": " + filters[i][2][j][1];
      }
      return value;
    }

    function clearAll() {
      filters.forEach(function (g) {
        state.chosen[g[0]] = [];
        Object.keys(boxes[g[0]]).forEach(function (v) { boxes[g[0]][v].checked = false; });
      });
      state.q = "";
      if (searchInput) searchInput.value = "";
      apply();
    }

    function chip(text, onRemove) {
      var b = el("button", "sx-chip");
      b.type = "button";
      b.setAttribute("aria-label", uiText("Remove {name}", {name: text}));
      b.appendChild(el("span", null, text));
      var x = el("span", "sx-chip-x", "×");
      x.setAttribute("aria-hidden", "true");
      b.appendChild(x);
      b.addEventListener("click", onRemove);
      return b;
    }

    function go() {
      // server mode: the address carries the choices; a new choice starts again at page one
      var url = new URL(location.href);
      filters.forEach(function (g) {
        url.searchParams.delete(g[0]);
        state.chosen[g[0]].forEach(function (v) { url.searchParams.append(g[0], v); });
      });
      var s = sorts[state.sort];
      if (s) {
        url.searchParams.set("sort", s[0]);
        url.searchParams.set("dir", s[2] > 0 ? "asc" : "desc");
      }
      url.searchParams.delete("page");
      location.href = url.toString();
    }

    function apply() {
      if (server && ready) { go(); return; }
      var shown = 0, active = 0;
      filters.forEach(function (g) { active += state.chosen[g[0]].length; });
      if (server) shown = rows.length;
      rows.forEach(function (row) {
        if (server) return;
        var ok = true;
        filters.forEach(function (g) {
          var chosen = state.chosen[g[0]];
          if (chosen.length && chosen.indexOf(row.getAttribute("data-" + g[0])) < 0) ok = false;
        });
        if (ok && state.q) {
          var hay = find.map(function (k) { return row.getAttribute("data-" + k) || ""; }).join(" ").toLowerCase();
          if (hay.indexOf(state.q) < 0) ok = false;
        }
        row.hidden = !ok;
        if (ok) shown++;
      });

      var s = sorts[state.sort];
      if (s && !server) {
        var key = "data-" + s[0];
        rows.sort(function (a, b) {
          var x = a.getAttribute(key) || "", y = b.getAttribute(key) || "";
          var c = s[1] === "num" ? parseFloat(x) - parseFloat(y) : x.localeCompare(y);
          return (c * s[2]) || (a._order - b._order);
        });
        rows.forEach(function (row) { list.appendChild(row); });
      }

      items.forEach(function (item, i) {
        item.setAttribute("aria-checked", String(i === state.sort));
        item.classList.toggle("is-on", i === state.sort);
      });
      sortMenu.label.textContent = s ? s[3] : uiText("Sort");
      headers.forEach(function (th) {
        var on = s && th.getAttribute("data-sort") === s[0];
        if (on) th.setAttribute("aria-sort", s[2] > 0 ? "ascending" : "descending"); else th.removeAttribute("aria-sort");
        th.querySelector(".sx-th-arrow").textContent = on ? (s[2] > 0 ? "↑" : "↓") : "↕";
      });
      if (filterCount) { filterCount.textContent = String(active); filterCount.hidden = !active; }
      if (filterLabel) filterLabel.parentNode.classList.toggle("is-on", active > 0);

      while (chips.firstChild) chips.removeChild(chips.firstChild);
      if (state.q) chips.appendChild(chip(uiText("Search: {q}", {q: state.q}), function () { state.q = ""; if (searchInput) searchInput.value = ""; apply(); }));
      filters.forEach(function (g) {
        state.chosen[g[0]].slice().forEach(function (v) {
          chips.appendChild(chip(labelOf(g[0], v), function () { boxes[g[0]][v].checked = false; state.chosen[g[0]].splice(state.chosen[g[0]].indexOf(v), 1); apply(); }));
        });
      });
      if (chips.firstChild) {
        var all = el("button", "btn btn-link btn-sm p-0", uiText("Clear all"));
        all.type = "button";
        all.addEventListener("click", clearAll);
        chips.appendChild(all);
      }
      chips.hidden = !chips.firstChild;
      empty.hidden = server || shown !== 0 || rows.length === 0;
      var whole = rows.length;
      if (server) {
        shown = parseInt(box.getAttribute("data-shown") || "0", 10);
        whole = parseInt(box.getAttribute("data-total") || "0", 10);
      }
      var count = {shown: shown, whole: whole, noun: noun[whole === 1 ? 0 : 1]};
      status.textContent = (shown === whole ? uiText("{whole} {noun}", count) : uiText("{shown} of {whole} {noun} shown", count)) +
        (s ? " · " + s[3].toLowerCase() : "");
    }

    apply();
    ready = true;
  }

  Array.prototype.forEach.call(document.querySelectorAll("[data-list-tools]"), init);
})();
