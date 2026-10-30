/* P29 file viewer: keyboard, zoom and text size, and PDFs drawn with the vendored pdf.js.
   Nothing is stored or sent anywhere but back to this portal. */
const root = document.querySelector('.pp-viewer');
const go = selector => {
  const link = document.querySelector(selector);
  if (link) window.location.href = link.href;
};

let zoom = 1;
let draw = () => {
  const image = root.querySelector('.pp-v-image');
  if (image) image.style.width = `${zoom * 100}%`;
};
const zoomLabel = root.querySelector('[data-zoom-level]');
const setZoom = value => {
  zoom = Math.min(3, Math.max(0.5, value));
  if (zoomLabel) zoomLabel.textContent = `${Math.round(zoom * 100)}%`;
  draw();
};
root.querySelector('[data-zoom-in]')?.addEventListener('click', () => setZoom(zoom + 0.25));
root.querySelector('[data-zoom-out]')?.addEventListener('click', () => setZoom(zoom - 0.25));

const sheet = root.querySelector('.pp-textsheet');
let textSize = 1;
const setText = value => {
  textSize = Math.min(1.8, Math.max(0.8, value));
  sheet.style.fontSize = `${textSize}rem`;
};
root.querySelector('[data-text-larger]')?.addEventListener('click', () => setText(textSize + 0.1));
root.querySelector('[data-text-smaller]')?.addEventListener('click', () => setText(textSize - 0.1));

let page = 1;
let pages = 0;
const turn = step => {
  if (!pages) return;
  page = Math.min(pages, Math.max(1, page + step));
  draw();
};

document.addEventListener('keydown', event => {
  if (event.target.closest('button, a, input')) {
    if (event.key === 'Escape') go('[data-viewer-close]');
    return;
  }
  if (event.key === 'Escape') go('[data-viewer-close]');
  else if (event.key === 'ArrowLeft') go('[data-viewer-prev]');
  else if (event.key === 'ArrowRight') go('[data-viewer-next]');
  else if (event.key === 'PageUp') turn(-1);
  else if (event.key === 'PageDown') turn(1);
});

const holder = root.querySelector('[data-pdf-src]');
if (holder) {
  const status = holder.querySelector('[data-pdf-status]');
  const canvas = holder.querySelector('canvas');
  const pageLabel = root.querySelector('[data-pdf-page]');
  const prev = root.querySelector('[data-pdf-prev]');
  const next = root.querySelector('[data-pdf-next]');
  try {
    const pdfjs = await import('../vendor/pdfjs/4.10.38/pdf.min.mjs');
    pdfjs.GlobalWorkerOptions.workerSrc = new URL('../vendor/pdfjs/4.10.38/pdf.worker.min.mjs', import.meta.url).href;
    // isEvalSupported off: pdf.js never builds code from a document's contents
    const doc = await pdfjs.getDocument({ url: holder.dataset.pdfSrc, isEvalSupported: false }).promise;
    pages = doc.numPages;
    let drawing = Promise.resolve();
    draw = () => {
      drawing = drawing.then(async () => {
        const ratio = window.devicePixelRatio || 1;
        const pdfPage = await doc.getPage(page);
        const viewport = pdfPage.getViewport({ scale: zoom * 1.4 * ratio });
        canvas.width = viewport.width;
        canvas.height = viewport.height;
        canvas.style.width = `${viewport.width / ratio}px`;
        await pdfPage.render({ canvasContext: canvas.getContext('2d'), viewport }).promise;
        pageLabel.textContent = `${page} / ${pages}`;
        prev.disabled = page <= 1;
        next.disabled = page >= pages;
        canvas.hidden = false;
        status.hidden = true;
        canvas.dataset.drawn = String(page);
      });
      return drawing;
    };
    prev.addEventListener('click', () => turn(-1));
    next.addEventListener('click', () => turn(1));
    await draw();
  } catch (error) {
    status.textContent = holder.dataset.failed;
    pageLabel.textContent = '-';
  }
}
