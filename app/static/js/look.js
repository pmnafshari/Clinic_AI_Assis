// P29 R-005: Preview over the file list. an image as an <img>, a text through textContent, a PDF drawn by the vendored
// pdf.js with isEvalSupported off, so it never builds code from a document. previous and next walk the list, the
// arrow keys too; Esc closes and the cursor goes back to the file it came from. nothing is kept in the browser.
const dialog = document.getElementById("look-dialog");
const buttons = [...document.querySelectorAll("button[data-look]")];
if (dialog && buttons.length && typeof dialog.showModal === "function") {
  const $ = name => dialog.querySelector(`[data-look-${name}]`);
  const body = $("body"), title = dialog.querySelector("#look-title"), pos = $("pos");
  const prev = $("prev"), next = $("next"), open = $("open"), download = $("download");
  const pages = $("pages"), pageLabel = $("page"), pagePrev = $("page-prev"), pageNext = $("page-next");
  let at = 0, opener = null, ticket = 0, turn = null, pdfjs = null;

  const say = words => {
    const p = document.createElement("p");
    p.className = "look-msg";
    p.textContent = words;
    body.replaceChildren(p);
  };
  const library = async () => {
    if (!pdfjs) {
      pdfjs = await import("../vendor/pdfjs/4.10.38/pdf.min.mjs");
      pdfjs.GlobalWorkerOptions.workerSrc = new URL("../vendor/pdfjs/4.10.38/pdf.worker.min.mjs", import.meta.url).href;
    }
    return pdfjs;
  };

  const show = async i => {
    at = i;
    const button = buttons[i], mine = ++ticket, kind = button.dataset.look, src = button.dataset.lookSrc;
    title.textContent = button.dataset.lookName;
    pos.textContent = `${i + 1} of ${buttons.length}`;
    const lost = document.activeElement;
    prev.disabled = i === 0;
    next.disabled = i === buttons.length - 1;
    if (lost && lost.disabled) (lost === next ? prev : next).focus();
    open.href = button.dataset.lookOpen;
    download.href = button.dataset.lookDownload;
    pages.hidden = true;
    turn = null;
    dialog.dataset.kind = kind;
    say("Loading...");
    const failed = () => { if (mine === ticket) say("This file could not be shown here. Download the original to open it."); };
    try {
      if (kind === "image") {
        const img = new Image();
        img.className = "look-image";
        img.alt = `Preview of ${button.dataset.lookName} (for orientation only)`;
        img.addEventListener("error", failed);
        img.src = src;
        body.replaceChildren(img);
      } else if (kind === "text") {
        const answer = await fetch(src, { credentials: "same-origin" });
        if (!answer.ok) throw new Error(String(answer.status));
        const words = await answer.text();
        if (mine !== ticket) return;
        const sheet = document.createElement("pre");
        sheet.className = "look-text";
        sheet.textContent = words;
        body.replaceChildren(sheet);
      } else if (kind === "pdf") {
        const lib = await library();
        const doc = await lib.getDocument({ url: src, isEvalSupported: false }).promise;
        if (mine !== ticket) return;
        const canvas = document.createElement("canvas");
        canvas.className = "look-canvas";
        body.replaceChildren(canvas);
        let page = 1;
        const draw = async () => {
          const sheet = await doc.getPage(page);
          const ratio = window.devicePixelRatio || 1;
          const fit = Math.max(200, body.clientWidth - 16) / sheet.getViewport({ scale: 1 }).width;
          const view = sheet.getViewport({ scale: fit * ratio });
          canvas.width = view.width;
          canvas.height = view.height;
          canvas.style.width = `${view.width / ratio}px`;
          await sheet.render({ canvasContext: canvas.getContext("2d"), viewport: view }).promise;
          if (mine !== ticket) return;
          pageLabel.textContent = `Page ${page} of ${doc.numPages}`;
          // a button that turns off would drop the cursor out of the viewer, so it moves to the other one
          const lost = document.activeElement;
          pagePrev.disabled = page <= 1;
          pageNext.disabled = page >= doc.numPages;
          if (lost && lost.disabled) (lost === pageNext ? pagePrev : pageNext).focus();
          canvas.dataset.drawn = String(page);
        };
        turn = step => { page = Math.min(doc.numPages, Math.max(1, page + step)); draw(); };
        pages.hidden = doc.numPages < 2;
        await draw();
      } else {
        say("No preview for this kind of file. Download the original to open it.");
      }
    } catch (error) {
      failed();
    }
  };

  buttons.forEach((button, i) => button.addEventListener("click", () => {
    opener = button;
    dialog.showModal();
    show(i);
  }));
  prev.addEventListener("click", () => { if (at > 0) show(at - 1); });
  next.addEventListener("click", () => { if (at < buttons.length - 1) show(at + 1); });
  pagePrev.addEventListener("click", () => turn && turn(-1));
  pageNext.addEventListener("click", () => turn && turn(1));
  $("close").addEventListener("click", () => dialog.close());
  document.addEventListener("keydown", event => {
    if (!dialog.open) return;
    if (event.key === "ArrowLeft" && at > 0) show(at - 1);
    else if (event.key === "ArrowRight" && at < buttons.length - 1) show(at + 1);
    else if (event.key === "PageUp" && turn) turn(-1);
    else if (event.key === "PageDown" && turn) turn(1);
    else return;
    event.preventDefault();
  });
  dialog.addEventListener("close", () => {
    ticket += 1;
    body.replaceChildren();
    if (opener) opener.focus();
  });
}
