// show/hide PIN (UX-10). Loaded on the login screen only.
//
// Identical contract to the staff login: ONE input whose type flips - never a
// second input holding the same value - with aria-pressed carrying the state
// and the label read off the element so it stays in the patient's language.
(function () {
  "use strict";
  var toggle = document.querySelector("[data-pw-toggle]");
  var input = document.querySelector("[data-pw-input]");
  if (!toggle || !input) return;
  toggle.addEventListener("click", function () {
    var shown = input.type === "text";
    input.type = shown ? "password" : "text";
    toggle.setAttribute("aria-pressed", shown ? "false" : "true");
    toggle.setAttribute("aria-label",
      shown ? toggle.dataset.labelShow : toggle.dataset.labelHide);
    var icon = toggle.querySelector("i");
    if (icon) icon.className = shown ? "bi bi-eye" : "bi bi-eye-slash";
  });
})();
