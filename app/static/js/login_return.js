"use strict";

// Fragments stay in the browser; carry the inherited anchor with this form only.
document.querySelectorAll("form[data-login-return]").forEach(function (form) {
  const target = form.elements.namedItem("next");
  if (target.value && !target.value.includes("#") && window.location.hash) {
    target.value += window.location.hash;
  }
});
