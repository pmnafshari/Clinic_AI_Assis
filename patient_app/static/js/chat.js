// pre-navigation feedback only. the form still does a normal POST and the
// browser still does a full-page render - there is no fetch call and no
// partial update anywhere in this file.

document.addEventListener("DOMContentLoaded", function () {
  var form = document.getElementById("chat-form");

  // the example chips fill the field and submit it. they are plain buttons -
  // nothing but the input is named "question", so the browser can only ever
  // serialise one value for it
  var chips = document.querySelectorAll(".chat-example");
  for (var i = 0; i < chips.length; i++) {
    chips[i].addEventListener("click", function () {
      var field = document.getElementById("question");
      if (field && form) {
        field.value = this.textContent.trim();
        form.requestSubmit();
      }
    });
  }

  if (form) {
    form.addEventListener("submit", function () {
      var button = document.getElementById("chat-submit");
      var label = button.querySelector(".chat-submit-label");
      var spinner = button.querySelector(".spinner-border");
      var help = document.getElementById("chat-pending-help");

      button.disabled = true;
      if (label) {
        label.textContent = button.dataset.pendingLabel;
      }
      if (spinner) {
        spinner.classList.remove("d-none");
      }
      if (help) {
        help.classList.remove("d-none");
      }
    });
  }

  // a keyboard or screen-reader user lands on the answer just returned,
  // instead of tabbing back down past the form to find it
  var heading = document.getElementById("chat-response-heading");
  if (heading) {
    heading.focus();
  }
});
