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

document.addEventListener("DOMContentLoaded", function () {
  initToasts();
  wireDropZones();
});
document.body.addEventListener("htmx:afterSwap", initToasts);
