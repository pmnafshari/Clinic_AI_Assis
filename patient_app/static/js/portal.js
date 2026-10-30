/* Patient-only progressive enhancement. No medical text is stored or sent here. */
(() => {
  const dialog = document.getElementById('patient-confirm');
  const message = document.getElementById('patient-confirm-message');
  let proceed = null;
  function confirmChoice(text, action) {
    if (!dialog || typeof dialog.showModal !== 'function') {
      if (window.confirm(text)) action();
      return;
    }
    message.textContent = text;
    proceed = action;
    dialog.showModal();
    dialog.querySelector('[data-dialog-cancel]').focus();
  }
  dialog.querySelector('[data-dialog-cancel]').addEventListener('click', () => dialog.close());
  dialog.querySelector('[data-dialog-confirm]').addEventListener('click', () => {
    const action = proceed;
    dialog.close();
    if (action) action();
  });
  dialog.addEventListener('close', () => { proceed = null; });
  document.querySelectorAll('form[data-confirm]').forEach(form => {
    form.addEventListener('submit', event => {
      event.preventDefault();
      confirmChoice(form.dataset.confirm, () => HTMLFormElement.prototype.submit.call(form));
    });
  });
  const changed = new Set();
  document.querySelectorAll('form').forEach(form => {
    const original = new URLSearchParams(new FormData(form)).toString();
    form.addEventListener('input', () => {
      const differs = new URLSearchParams(new FormData(form)).toString() !== original;
      differs ? changed.add(form) : changed.delete(form);
    });
  });
  document.querySelectorAll('[data-locale-link]').forEach(link => {
    link.addEventListener('click', event => {
      if (!changed.size) return;
      event.preventDefault();
      const copy = document.querySelector('script[data-unsaved]').dataset.unsaved;
      confirmChoice(copy, () => { window.location.href = link.href; });
    });
  });
  document.querySelectorAll('[data-count-for]').forEach(counter => {
    const field = document.getElementById(counter.dataset.countFor);
    const suffix = counter.textContent.replace(/^0 \/ \d+ /, '');
    const update = () => { counter.textContent = `${field.value.length} / ${field.maxLength} ${suffix}`; };
    field.addEventListener('input', update);
    update();
  });
  const filters = document.querySelector('[data-file-filters]');
  if (filters) {
    filters.hidden = false;
    const search = document.getElementById('file-search');
    const kind = document.getElementById('file-kind');
    const rows = [...document.querySelectorAll('[data-file-kind]')];
    const apply = () => {
      let matches = 0;
      for (const row of rows) {
        row.hidden = !!((kind.value && row.dataset.fileKind !== kind.value) || !row.textContent.toLocaleLowerCase().includes(search.value.trim().toLocaleLowerCase()));
        if (!row.hidden) matches++;
      }
      document.getElementById('file-no-matches').hidden = matches !== 0;
    };
    search.addEventListener('input', apply);
    kind.addEventListener('change', apply);
    document.getElementById('file-clear').addEventListener('click', () => { search.value = ''; kind.value = ''; apply(); search.focus(); });
  }
})();
