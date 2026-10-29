document.addEventListener('DOMContentLoaded', () => {
  // Same-tab navigation keeps its tab token. A newly opened tab starts a fresh login flow.
  document.querySelectorAll('a[target="_blank"]').forEach(link => {
    try {
      const targetUrl = new URL(link.href, window.location.href);
      targetUrl.searchParams.delete('__tab');
      link.href = targetUrl.toString();
    } catch (_) {}
  });

  const toggle = document.getElementById('menuToggle');
  const sidebar = document.getElementById('sidebar');
  if (toggle && sidebar) toggle.addEventListener('click', () => sidebar.classList.toggle('sidebar-open'));

  const globalSearch = document.getElementById('globalSearch');
  const vendorSearch = document.getElementById('vendorSearch');
  const vendorGrid = document.getElementById('vendorGrid');
  const vendorNoResults = document.getElementById('vendorNoResults');

  function applySearch() {
    const globalQuery = globalSearch ? globalSearch.value.trim().toLowerCase() : '';
    document.querySelectorAll('table').forEach(table => {
      const localQuery = (table.dataset.localSearch || '').trim().toLowerCase();
      const bodyRows = [...table.querySelectorAll('tbody tr:not(.search-empty-row)')];
      const emptyRow = table.querySelector('.search-empty-row');
      let visible = 0;
      bodyRows.forEach(row => {
        const text = row.innerText.toLowerCase();
        const show = (!globalQuery || text.includes(globalQuery)) && (!localQuery || text.includes(localQuery));
        row.hidden = !show;
        if (show) visible++;
      });
      const searching = Boolean(globalQuery || localQuery);
      if (emptyRow) emptyRow.hidden = !searching || visible !== 0;
      else if (searching && bodyRows.length && visible === 0) {
        const row = document.createElement('tr');
        row.className = 'search-empty-row';
        const cell = document.createElement('td');
        cell.colSpan = Math.max(1, table.querySelectorAll('thead th').length);
        cell.textContent = 'No records match the current search.';
        row.appendChild(cell);
        table.querySelector('tbody')?.appendChild(row);
      }
    });

    document.querySelectorAll('.metric-card, .report-card, .activity-item').forEach(card => {
      card.hidden = Boolean(globalQuery) && !card.innerText.toLowerCase().includes(globalQuery);
    });

    if (vendorGrid) {
      const localQuery = vendorSearch ? vendorSearch.value.trim().toLowerCase() : '';
      const cards = [...vendorGrid.querySelectorAll('.searchable-vendor')];
      let count = 0;
      cards.forEach(card => {
        const text = card.innerText.toLowerCase();
        const show = (!globalQuery || text.includes(globalQuery)) && (!localQuery || text.includes(localQuery));
        card.hidden = !show;
        if (show) count++;
      });
      if (vendorNoResults) vendorNoResults.hidden = !cards.length || count !== 0;
    }
  }

  if (globalSearch) {
    globalSearch.addEventListener('input', applySearch);
    globalSearch.addEventListener('keydown', event => {
      if (event.key === 'Escape') {
        globalSearch.value = '';
        applySearch();
      }
    });
  }
  if (vendorSearch) vendorSearch.addEventListener('input', applySearch);
  document.querySelectorAll('[data-table-search]').forEach(input => {
    const table = document.getElementById(input.dataset.tableSearch);
    if (!table) return;
    input.addEventListener('input', () => {
      table.dataset.localSearch = input.value;
      applySearch();
    });
  });

  document.querySelectorAll('.flash').forEach(el => {
    window.setTimeout(() => {
      if (el.isConnected) {
        el.style.opacity = '0';
        el.style.transform = 'translateY(-4px)';
        window.setTimeout(() => el.remove(), 250);
      }
    }, 6500);
  });

  document.querySelectorAll('form').forEach(form => {
    form.addEventListener('submit', event => {
      if (!form.checkValidity()) {
        event.preventDefault();
        form.reportValidity();
      }
    });
  });

  // Destructive administrative operations require the separate confirmation text in the form.
  // The browser confirmation is just a second guardrail; server-side checks remain authoritative.
});
