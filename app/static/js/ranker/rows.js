// One title on the board: building its row in the browser, and every way to move
// or remove it that is not a drag.

let undoAction = null;          // single level, cleared on reload — see UNDO below

// ---------------------------------------------------------------------------
// building a row in the browser
// ---------------------------------------------------------------------------
// Only for titles that arrive AFTER first paint — a search result dragged in, or
// a removal being undone. Everything else is rendered by _ranker_row.html.
// Titles come from a third-party catalogue and are untrusted, so every value
// here is either escaped or assigned through .textContent.

function buildRow(item) {
    const el = document.createElement('div');
    el.className = 'ranker-item';
    el.tabIndex = 0;
    el.setAttribute('role', 'listitem');
    el.dataset.key = item.key || (item.media + ':' + item.match_source + ':' + item.match_id);
    el.dataset.media = item.media || 'show';
    el.dataset.tmdb = item.tmdb || '';
    el.dataset.title = item.title || '';
    el.dataset.ref = JSON.stringify(item);
    const thumb = item.tmdb
        ? '<img class="ranker-thumb" width="46" height="69" loading="lazy" alt="" src="' +
          esc('/api/rankings/poster?media=' + el.dataset.media + '&tmdb=' + item.tmdb) +
          '" onerror="this.classList.add(\'is-missing\'); this.removeAttribute(\'src\');">'
        : '<span class="ranker-thumb is-missing" aria-hidden="true"></span>';
    el.innerHTML =
        '<span class="ranker-grip" aria-hidden="true">⠿</span>' + thumb +
        '<span class="ranker-name"><span class="ranker-title">' +
        esc(item.title || 'Untitled') + '</span>' +
        (item.year ? '<span class="ranker-year">' + esc(item.year) + '</span>' : '') +
        '</span><span class="ranker-facts">' +
        (item.season_count ? '<span class="ranker-fact">' + esc(item.season_count) + 'S</span>' : '') +
        (item.episode_count ? '<span class="ranker-fact">' + esc(item.episode_count) + 'E</span>' : '') +
        (item.runtime ? '<span class="ranker-fact">' + esc(item.runtime) + 'm</span>' : '') +
        (item.network ? '<span class="ranker-fact network">' + esc(item.network) + '</span>' : '') +
        (item.user_rating ? '<span class="ranker-fact rating">★' + esc(item.user_rating) + '</span>' : '') +
        '</span><span class="ranker-actions">' +
        '<button type="button" class="ranker-act" title="Move up" aria-label="Move up" onclick="nudgeItem(this, -1)">▲</button>' +
        '<button type="button" class="ranker-act" title="Move down" aria-label="Move down" onclick="nudgeItem(this, 1)">▼</button>' +
        '<button type="button" class="ranker-act" title="Move to tier" aria-label="Move to tier" onclick="openMoveMenu(this)">⇄</button>' +
        '<button type="button" class="ranker-act danger" title="Remove from board" aria-label="Remove from board" onclick="removeItem(this)">✕</button>' +
        '</span>';
    return el;
}

// ---------------------------------------------------------------------------
// keyboard and non-pointer moves
// ---------------------------------------------------------------------------
// Every reorder a drag can do is reachable from here, which is what keeps the
// vendored drag library a convenience rather than a dependency.

function nudgeItem(button, delta) {
    const row = button.closest('.ranker-item');
    const container = row.parentElement;
    const siblings = Array.from(container.querySelectorAll(':scope > .ranker-item'));
    const index = siblings.indexOf(row);
    const target = index + delta;
    if (target < 0 || target >= siblings.length) return;
    if (delta < 0) container.insertBefore(row, siblings[target]);
    else container.insertBefore(row, siblings[target].nextSibling);
    row.focus();
    scheduleSave();
}

function openMoveMenu(button) {
    const row = button.closest('.ranker-item');
    const menu = document.getElementById('moveMenu');
    menu.innerHTML = '';
    const destinations = [{ uid: null, label: 'Pool (unranked)' }].concat(
        (state.board.categories || []).map(c => ({ uid: c.uid, label: c.label || 'Untitled' })));
    destinations.forEach(dest => {
        const item = document.createElement('button');
        item.type = 'button';
        item.textContent = dest.label;
        item.onclick = () => { closeMoveMenu(); moveRowTo(row, dest.uid); };
        menu.appendChild(item);
    });
    const box = button.getBoundingClientRect();
    menu.hidden = false;
    menu.style.top = (window.scrollY + box.bottom + 4) + 'px';
    menu.style.left = Math.max(8, window.scrollX + box.right - menu.offsetWidth) + 'px';
}

function closeMoveMenu() {
    const menu = document.getElementById('moveMenu');
    if (menu) menu.hidden = true;
}

async function moveRowTo(row, categoryUid) {
    const container = categoryUid === null
        ? document.getElementById('rankerPool')
        : await openTierBody(categoryUid);
    if (!container) return;
    container.appendChild(row);
    refreshCounts();
    scheduleSave();
}

// Opens a tier and makes sure its rows are actually on the page, so a title
// moved into a tier nobody has opened lands somewhere real rather than into a
// container whose contents the client has never seen.
async function openTierBody(categoryUid) {
    const details = document.querySelector('.ranker-tier[data-uid="' + CSS.escape(categoryUid) + '"]');
    const body = document.getElementById('tierBody-' + categoryUid);
    if (!body) return null;
    if (details) details.open = true;
    if (body.dataset.loaded !== '1') {
        try {
            const res = await fetch('/rankings/fragments/tier?board=' +
                encodeURIComponent(boardUid()) + '&tier=' + encodeURIComponent(categoryUid));
            body.innerHTML = await res.text();
            body.dataset.loaded = '1';
            initSortable(body);
        } catch (e) {
            toast('Could not open that tier.', false);
            return null;
        }
    }
    return body;
}

// ---------------------------------------------------------------------------
// undo — DESTRUCTIVE ACTIONS ONLY
// ---------------------------------------------------------------------------
// Moving a title between tiers undoes itself: drag it back. What dragging cannot
// reverse is taking a title off the board and deleting a tier, so those two get
// a toast with an Undo. One level, held in the browser, gone on reload.

function offerUndo(message, action) {
    undoAction = action;
    let host = document.getElementById('toastHost');
    if (!host) { host = document.createElement('div'); host.id = 'toastHost'; document.body.appendChild(host); }
    const t = document.createElement('div');
    t.className = 'toast ok';
    t.style.pointerEvents = 'auto';
    t.textContent = message + '  ';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn-ghost small';
    btn.textContent = 'Undo';
    btn.onclick = async () => { t.remove(); const run = undoAction; undoAction = null; if (run) await run(); };
    t.appendChild(btn);
    host.appendChild(t);
    requestAnimationFrame(() => t.classList.add('show'));
    setTimeout(() => { t.classList.remove('show'); setTimeout(() => t.remove(), 300); }, 9000);
}

function removeItem(button) {
    const row = button.closest('.ranker-item');
    const key = row.dataset.key;
    let ref = null;
    try { ref = JSON.parse(row.dataset.ref); } catch (e) { ref = null; }
    const container = row.parentElement;
    const index = Array.from(container.children).indexOf(row);
    confirmInline(button, 'Take this title off the board?', async () => {
        try {
            const removal = await api(
                '/api/rankings/boards/' + encodeURIComponent(boardUid()) + '/items',
                'DELETE', { keys: [key] });
            if (removal.removed) bumpVersion();
        } catch (e) { toast(e.message, false); return; }
        row.remove();
        state.board.pool = (state.board.pool || []).filter(k => k !== key);
        (state.board.categories || []).forEach(c => { c.items = c.items.filter(k => k !== key); });
        refreshCounts();
        offerUndo('Removed from the board.', async () => {
            if (!ref) { window.location.reload(); return; }
            try {
                const added = await api(
                    '/api/rankings/boards/' + encodeURIComponent(boardUid()) + '/items',
                    'POST', { refs: [ref] });
                if (added.added) bumpVersion();
            } catch (e) { toast(e.message, false); return; }
            const restored = buildRow(ref);
            container.insertBefore(restored, container.children[index] || null);
            refreshCounts();
            scheduleSave();
        });
    }, { danger: true });
}

document.addEventListener('click', (event) => {
    if (!event.target.closest('#moveMenu') && !event.target.closest('.ranker-act')) closeMoveMenu();
});
