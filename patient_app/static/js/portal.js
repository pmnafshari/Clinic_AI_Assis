/* Patient-only progressive enhancement. No medical text is stored or sent here. */
(() => {
  const dialog = document.getElementById('patient-confirm');
  const message = document.getElementById('patient-confirm-message');
  let proceed = null;
  const yesButton = dialog.querySelector('[data-dialog-confirm]');
  const noButton = dialog.querySelector('[data-dialog-cancel]');
  const yesLabel = yesButton.textContent;
  const noLabel = noButton.textContent;
  // a form may name its own answers and mark a cancelling action (P29)
  function confirmChoice(text, action, form) {
    if (!dialog || typeof dialog.showModal !== 'function') {
      if (window.confirm(text)) action();
      return;
    }
    message.textContent = text;
    yesButton.textContent = (form && form.dataset.confirmYes) || yesLabel;
    noButton.textContent = (form && form.dataset.confirmNo) || noLabel;
    dialog.classList.toggle('is-danger', !!(form && 'confirmDanger' in form.dataset));
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
      confirmChoice(form.dataset.confirm, () => HTMLFormElement.prototype.submit.call(form), form);
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
  // P29: details panels open from their row; close with the button or Escape
  document.querySelectorAll('[data-dialog-open]').forEach(button => {
    const panel = document.getElementById(button.dataset.dialogOpen);
    if (!panel || typeof panel.showModal !== 'function') return;
    button.hidden = false;
    button.addEventListener('click', () => panel.showModal());
  });
  document.querySelectorAll('[data-dialog-close]').forEach(button => {
    button.addEventListener('click', () => button.closest('dialog').close());
  });
  // P29: tabs that show one group of table rows at a time
  document.querySelectorAll('[data-appt-tabs], [data-bill-tabs]').forEach(tabs => {
    const table = tabs.closest('section').querySelector('tbody');
    tabs.hidden = false;
    tabs.querySelectorAll('button').forEach(tab => {
      tab.addEventListener('click', () => {
        tabs.querySelectorAll('button').forEach(other => other.setAttribute('aria-pressed', String(other === tab)));
        const group = tab.dataset.apptTab || tab.dataset.billTab;
        table.querySelectorAll('tr[data-group]').forEach(row => { row.hidden = group !== 'all' && row.dataset.group !== group; });
      });
    });
  });
  // P29: one invoice's detail at a time; without script every detail stays on the page
  const invoices = [...document.querySelectorAll('[data-invoice]')];
  if (invoices.length) {
    const show = id => invoices.forEach(section => { section.hidden = section.id !== id; });
    show(invoices[0].id);
    document.querySelectorAll('[data-invoice-open]').forEach(link => {
      link.addEventListener('click', event => {
        event.preventDefault();
        const id = link.getAttribute('aria-controls');
        show(id);
        const target = document.getElementById(id);
        target.scrollIntoView({ block: 'start' });
        target.focus({ preventScroll: true });
      });
    });
  }
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
    // P29: sort, and tiles or rows - the choice of layout is remembered on this device only
    const list = document.querySelector('.pp-files');
    const sort = document.getElementById('file-sort');
    sort.addEventListener('change', () => {
      const by = { new: (a, b) => b.dataset.date.localeCompare(a.dataset.date),
                   old: (a, b) => a.dataset.date.localeCompare(b.dataset.date),
                   name: (a, b) => a.dataset.name.localeCompare(b.dataset.name) }[sort.value];
      rows.slice().sort(by).forEach(row => list.appendChild(row));
    });
    const modes = [...filters.querySelectorAll('[data-file-mode]')];
    const setMode = mode => {
      list.dataset.mode = mode;
      modes.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.fileMode === mode)));
      try { localStorage.setItem('pp-files-view', mode); } catch (error) { /* private window: not remembered */ }
    };
    modes.forEach(button => button.addEventListener('click', () => setMode(button.dataset.fileMode)));
    try { if (localStorage.getItem('pp-files-view') === 'list') setMode('list'); } catch (error) { /* not remembered */ }
  }
})();
