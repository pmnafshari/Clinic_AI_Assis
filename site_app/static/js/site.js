// Site chrome only. No data, no fetch, no state that matters.
(function () {
  "use strict";

  // sticky header: a class toggled past a threshold, so the background and
  // shadow can transition. css alone cannot express "after scrolling".
  var header = document.querySelector("[data-site-header]");
  if (header) {
    var onScroll = function () {
      header.classList.toggle("is-stuck", window.scrollY > 8);
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
  }

  // mobile menu. the button owns aria-expanded and the panel owns hidden, so
  // the accessible state and the visible state cannot drift apart.
  var toggle = document.querySelector("[data-menu-toggle]");
  var menu = document.querySelector("[data-menu]");
  if (toggle && menu) {
    var setOpen = function (open) {
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      menu.hidden = !open;
    };
    toggle.addEventListener("click", function () {
      setOpen(toggle.getAttribute("aria-expanded") !== "true");
    });
    // escape closes and returns focus to the button - without the focus move
    // a keyboard user is left inside a panel that is no longer there
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && toggle.getAttribute("aria-expanded") === "true") {
        setOpen(false);
        toggle.focus();
      }
    });
    menu.addEventListener("click", function (e) {
      if (e.target.tagName === "A") { setOpen(false); }
    });
  }
})();
