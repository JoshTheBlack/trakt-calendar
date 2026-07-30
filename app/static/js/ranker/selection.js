// ---------------------------------------------------------------------------
// bulk selection in the pool
// ---------------------------------------------------------------------------
// Curating a few hundred candidates one drag at a time is the difference between
// this being pleasant and being a chore.

let selection = [];             // pool keys, in click order, for the bulk move
let lastClickedKey = null;      // anchor for shift-click range selection

function onPoolClick(event) {
    const row = event.target.closest('.ranker-item');
    if (!row || event.target.closest('.ranker-act')) return;
    if (!event.shiftKey && !event.ctrlKey && !event.metaKey) return;
    event.preventDefault();
    const rows = Array.from(document.querySelectorAll('#rankerPool > .ranker-item'));
    if (event.shiftKey && lastClickedKey) {
        const from = rows.findIndex(r => r.dataset.key === lastClickedKey);
        const to = rows.indexOf(row);
        if (from >= 0 && to >= 0) {
            rows.slice(Math.min(from, to), Math.max(from, to) + 1)
                .forEach(r => { if (!selection.includes(r.dataset.key)) selection.push(r.dataset.key); });
        }
    } else {
        const at = selection.indexOf(row.dataset.key);
        if (at >= 0) selection.splice(at, 1); else selection.push(row.dataset.key);
    }
    lastClickedKey = row.dataset.key;
    renderSelection();
}

function renderSelection() {
    const chosen = new Set(selection);
    document.querySelectorAll('#rankerPool > .ranker-item').forEach(row => {
        row.classList.toggle('is-selected', chosen.has(row.dataset.key));
    });
    const bar = document.getElementById('poolBulk');
    if (!bar) return;
    bar.hidden = selection.length === 0;
    document.getElementById('poolSelectedCount').textContent = selection.length + ' selected';
    const target = document.getElementById('bulkTarget');
    if (target && !target.options.length) {
        (state.board.categories || []).forEach(cat => {
            const option = document.createElement('option');
            option.value = cat.uid;
            option.textContent = cat.label || 'Untitled';
            target.appendChild(option);
        });
    }
}

function clearSelection() { selection = []; lastClickedKey = null; renderSelection(); }

async function moveSelectionToTier() {
    const target = document.getElementById('bulkTarget').value;
    if (!target || !selection.length) return;
    const body = await openTierBody(target);
    if (!body) return;
    selection.forEach(key => {
        const row = document.querySelector('#rankerPool > .ranker-item[data-key="' + CSS.escape(key) + '"]');
        if (row) body.appendChild(row);
    });
    clearSelection();
    refreshCounts();
    scheduleSave();
}
