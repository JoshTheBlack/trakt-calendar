// The rankings page: boards, the unranked pool, tier accordions, drag-and-drop,
// auto-save, and the poster-grid export modal.
//
// THREE RULES THIS FILE IS BUILT AROUND.
//
// 1. EVERY MUTATION IS A fetch WITH A JSON BODY. The app-wide request-shape
//    middleware refuses any POST/PUT/PATCH/DELETE that is not exactly
//    application/json, which is deliberate CSRF defence. htmx's hx-boost submits
//    a form with its native urlencoded encoding, so a boosted POST would be
//    refused with 415 — boost is for GET navigation only, and it is used here on
//    the board switcher and nowhere else.
//
// 2. DRAG BINDING IS IDEMPOTENT AND RE-RUNS AFTER EVERY SWAP. htmx replaces the
//    containers Sortable is bound to — the whole board on a boosted switch, a
//    tier's body when it is first opened. Binding a container twice produces two
//    drop handlers and therefore two saves of the same drag, which is a data bug
//    rather than a visual one. initSortable() may be called on any subtree, any
//    number of times.
//
// 3. THE LAYOUT IS READ AT SAVE TIME, NOT AT DRAG TIME. A save in flight while a
//    fragment swaps must not lose a drag, so nothing is snapshotted early; the
//    board's `version` arbitrates, and a 409 reloads rather than guessing.

function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// Same toast() as app.js and distrakt.js. Kept local for the reason distrakt.js
// gives: this page loads its own script rather than pulling in the calendar's.
function toast(message, ok) {
    let host = document.getElementById('toastHost');
    if (!host) { host = document.createElement('div'); host.id = 'toastHost'; document.body.appendChild(host); }
    const t = document.createElement('div');
    t.className = 'toast ' + (ok ? 'ok' : 'err');
    t.textContent = message;
    host.appendChild(t);
    while (host.children.length > 6) host.firstChild.remove();
    requestAnimationFrame(() => t.classList.add('show'));
    setTimeout(() => { t.classList.remove('show'); setTimeout(() => t.remove(), 300); }, 4200);
}

// ---------------------------------------------------------------------------
// state
// ---------------------------------------------------------------------------
// `board` mirrors what the server rendered: every tier with its settings and its
// item keys, plus the pool. It exists because a CLOSED tier draws no rows, so
// the DOM alone cannot describe the board — and save_layout refuses a payload
// that omits a tier and would overwrite a label the payload got wrong.

let state = { board: null, sources: {}, username: '', limits: null, template: [], poolPageSize: 60 };
let saveTimer = null;
let saving = false;
let dirty = false;
let undoAction = null;          // single level, cleared on reload — see UNDO below
let selection = [];             // pool keys, in click order, for the bulk move
let lastClickedKey = null;      // anchor for shift-click range selection
let previewUrl = null;          // the object URL currently in the preview <img>
let previewTimer = null;
let searchTimer = null;
const sortables = new Map();    // element -> Sortable instance, so a detached one can be destroyed

function readPageData() {
    const node = document.getElementById('rankerData');
    if (!node) { state.board = null; return; }
    try {
        const data = JSON.parse(node.textContent);
        state = Object.assign(state, data);
    } catch (e) {
        toast('This board could not be read — reload the page.', false);
    }
}

function boardUid() { return state.board && state.board.uid; }

// Every write the data layer accepts bumps the board's version, and the next
// save has to echo the current one. Called ONLY where the server actually
// changed something — adding a title the board already holds is accepted and
// changes nothing, and guessing a bump there would make the next save a 409.
function bumpVersion() { state.board.version += 1; }

function tierByUid(uid) { return (state.board.categories || []).find(c => c.uid === uid) || null; }

// ---------------------------------------------------------------------------
// asking the user something
// ---------------------------------------------------------------------------
// Nothing here calls prompt(), confirm() or alert(). They block the page, they
// cannot be made to look like the rest of it, and a browser configured to
// suppress them turns "name your board" into a dead button. One in-page dialog
// serves both shapes and resolves a promise, so the callers still read as
// straight-line code.

let askResolve = null;

// Resolves to the typed string (asking for text), to true (asking to confirm),
// or to null when it was dismissed — so `=== null` is always "cancelled" and an
// empty string stays a real answer.
function ask(options) {
    const modal = document.getElementById('askModal');
    const field = document.getElementById('askField');
    const input = document.getElementById('askInput');
    const message = document.getElementById('askMessage');
    document.getElementById('askTitle').textContent = options.title || '';
    document.getElementById('askOk').textContent = options.confirmText || 'OK';
    message.textContent = options.message || '';
    message.hidden = !options.message;
    const wantsText = options.input !== false;
    field.hidden = !wantsText;
    if (wantsText) {
        document.getElementById('askLabel').textContent = options.label || 'Name';
        input.maxLength = options.maxlength || 60;
        input.value = options.value || '';
    }
    modal.classList.add('open');
    if (wantsText) { input.focus(); input.select(); }
    return new Promise(resolve => { askResolve = resolve; });
}

function closeAsk(answer) {
    document.getElementById('askModal').classList.remove('open');
    const resolve = askResolve;
    askResolve = null;
    if (resolve) resolve(answer);
}

function submitAsk() {
    const wantsText = !document.getElementById('askField').hidden;
    closeAsk(wantsText ? document.getElementById('askInput').value.trim() : true);
}

// ---------------------------------------------------------------------------
// talking to the API
// ---------------------------------------------------------------------------

async function api(url, method, body) {
    const opts = { method: method || 'GET' };
    if (method && method !== 'GET') {
        // Including DELETE: the middleware wants a JSON content type on every
        // mutating request, so one that has nothing to say sends {}.
        opts.headers = { 'Content-Type': 'application/json' };
        opts.body = JSON.stringify(body || {});
    }
    const res = await fetch(url, opts);
    let data = {};
    try { data = await res.json(); } catch (e) { data = {}; }
    if (!res.ok || data.ok === false) {
        const err = new Error(data.error || ('Request failed (' + res.status + ')'));
        err.status = res.status;
        err.data = data;
        throw err;
    }
    return data;
}

// ---------------------------------------------------------------------------
// drag and drop
// ---------------------------------------------------------------------------

function keysIn(container) {
    if (!container) return [];
    return Array.from(container.querySelectorAll(':scope > .ranker-item'))
        .map(el => el.dataset.key);
}

// Every container that holds draggable titles: the pool and each tier's body.
// Search results are bound separately — they clone rather than move.
function dropTargets(root) {
    const scope = root && root.querySelectorAll ? root : document;
    const found = Array.from(scope.querySelectorAll('.ranker-pool, .ranker-rows'));
    if (scope.matches && scope.matches('.ranker-pool, .ranker-rows')) found.push(scope);
    return found;
}

// SAFE TO CALL ON ANY SUBTREE, ANY NUMBER OF TIMES. A container that is already
// bound is skipped, so the load-time call and the per-swap call cannot between
// them install two sets of handlers on the same element.
function initSortable(root) {
    if (typeof Sortable === 'undefined') return;
    for (const [el, instance] of sortables) {
        if (!el.isConnected) { instance.destroy(); sortables.delete(el); }
    }
    dropTargets(root).forEach(container => {
        if (sortables.has(container)) return;
        sortables.set(container, new Sortable(container, {
            group: 'ranker',
            animation: 140,
            draggable: '.ranker-item',
            handle: '.ranker-item',
            filter: '.ranker-act',
            preventOnFilter: false,
            ghostClass: 'sortable-ghost',
            dragClass: 'sortable-drag',
            onEnd: onDragEnd,
        }));
    });
    initSearchDrag();
}

function initSearchDrag() {
    const results = document.getElementById('searchResults');
    if (!results || sortables.has(results)) return;
    sortables.set(results, new Sortable(results, {
        // `pull: 'clone'` with `put: false`: a result dragged into a tier leaves
        // the list intact, because the list is a view of a search rather than a
        // place titles live.
        group: { name: 'ranker', pull: 'clone', put: false },
        sort: false,
        draggable: '.ranker-result:not(.is-held)',
        animation: 140,
        onEnd: onSearchDragEnd,
    }));
}

function onDragEnd(evt) {
    if (evt.from === evt.to && evt.oldIndex === evt.newIndex) return;
    refreshCounts();
    scheduleSave();
}

// A search result dropped straight into a tier: add it to the board first, then
// replace the clone with a real row so it is a title like any other.
async function onSearchDragEnd(evt) {
    const dropped = evt.item;
    if (!dropped || !dropped.parentElement || dropped.parentElement.id === 'searchResults') return;
    let ref;
    try { ref = JSON.parse(dropped.dataset.ref); } catch (e) { dropped.remove(); return; }
    const target = dropped.parentElement;
    const index = Array.from(target.children).indexOf(dropped);
    dropped.remove();
    try {
        const added = await api(
            '/api/rankings/boards/' + encodeURIComponent(boardUid()) + '/items',
            'POST', { refs: [ref] });
        if (added.added) bumpVersion();
    } catch (e) {
        toast(e.message, false);
        return;
    }
    const row = buildRow(ref);
    target.insertBefore(row, target.children[index] || null);
    markHeldResults();
    refreshCounts();
    scheduleSave();
}

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
// saving
// ---------------------------------------------------------------------------

// DOM -> state, for the containers the DOM is authoritative for. A tier whose
// rows were never fetched keeps the keys the server sent; one that is on screen
// is described by what is on screen.
function syncLoadedContainers() {
    const placed = new Set();
    (state.board.categories || []).forEach(cat => {
        const body = document.getElementById('tierBody-' + cat.uid);
        if (body && body.dataset.loaded === '1') cat.items = keysIn(body);
        cat.items.forEach(key => placed.add(key));
    });
    // The pool is what is on screen, followed by whatever has not been paged in
    // yet and has not since been dragged into a tier. Its order carries no
    // meaning — every pooled title is stored at the same rank — so appending the
    // unseen tail rather than interleaving it loses nothing.
    const shown = keysIn(document.getElementById('rankerPool'));
    const seen = new Set(shown);
    state.board.pool = shown.concat(
        (state.board.pool || []).filter(key => !seen.has(key) && !placed.has(key)));
}

function readLayout() {
    syncLoadedContainers();
    return {
        version: state.board.version,
        // EVERY tier on the board, always: save_layout refuses a payload that
        // omits one, because "renormalize sort_order to a dense 0..N-1" is only
        // well defined over the full set.
        categories: (state.board.categories || []).map(cat => ({
            uid: cat.uid, label: cat.label, rank_priority: cat.rank_priority,
            is_isolated: cat.is_isolated, colour: cat.colour, items: cat.items.slice(),
        })),
        pool: state.board.pool.slice(),
    };
}

function setSaveState(value) {
    const el = document.getElementById('saveState');
    if (!el) return;
    el.dataset.state = value;
    el.textContent = { idle: '', saving: 'Saving…', saved: 'Saved', error: 'Not saved' }[value] || '';
}

// Coalesces a run of quick drags into one write. The delay is also what makes a
// drag-drag-drag sequence cost one request rather than three.
function scheduleSave() {
    dirty = true;
    setSaveState('saving');
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveNow, 800);
}

async function saveNow() {
    if (!state.board) return;
    if (saving) { scheduleSave(); return; }
    saving = true;
    dirty = false;
    try {
        const payload = readLayout();
        const data = await api('/api/rankings/boards/' + encodeURIComponent(boardUid()) + '/save',
                               'POST', payload);
        state.board.version = data.version;
        setSaveState(dirty ? 'saving' : 'saved');
    } catch (e) {
        if (e.status === 409) {
            // Another tab saved first. Reloading is the honest answer: guessing
            // which arrangement wins would silently discard somebody's work.
            setSaveState('error');
            toast('This board changed in another tab — reloading it.', false);
            setTimeout(() => window.location.reload(), 1200);
            return;
        }
        setSaveState('error');
        toast(e.message, false);
    } finally {
        saving = false;
    }
}

function refreshCounts() {
    const pool = document.getElementById('rankerPool');
    const poolCount = document.getElementById('poolCount');
    if (pool && poolCount) {
        // What is on screen plus what has not been paged in yet.
        const shown = keysIn(pool).length;
        const unseen = Math.max(0, (state.board.pool || []).length - shown);
        poolCount.textContent = shown + unseen;
    }
    document.querySelectorAll('.ranker-tier').forEach(tier => {
        const body = document.getElementById('tierBody-' + tier.dataset.uid);
        const count = tier.querySelector('.ranker-tier-meta .ranker-count');
        if (!body || !count) return;
        count.textContent = body.dataset.loaded === '1'
            ? keysIn(body).length
            : (tierByUid(tier.dataset.uid) || { items: [] }).items.length;
    });
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

function deleteTier(button, uid) {
    const cat = tierByUid(uid);
    if (!cat) return;
    const details = document.querySelector('.ranker-tier[data-uid="' + CSS.escape(uid) + '"]');
    const snapshot = Object.assign({}, cat, { items: cat.items.slice() });
    confirmInline(button,
        'Delete this tier? Its titles go back to the pool.', async () => {
        // The rows are fetched FIRST, before anything is deleted. They are the
        // titles about to land in the pool, and moving the elements that already
        // exist is what puts them on screen without a reload — a tier nobody had
        // opened has no rows to move.
        const body = await openTierBody(uid);
        try {
            await api('/api/rankings/boards/' + encodeURIComponent(boardUid()) +
                      '/categories/' + encodeURIComponent(uid), 'DELETE', {});
        } catch (e) { toast(e.message, false); return; }
        // Straight into the pool, keeping each row's own element — so its
        // artwork is already loaded and its `data-ref` still describes it.
        const pool = document.getElementById('rankerPool');
        if (body && pool) {
            Array.from(body.querySelectorAll(':scope > .ranker-item'))
                .forEach(row => pool.appendChild(row));
        }
        const empty = document.getElementById('poolEmpty');
        if (empty) empty.remove();
        state.board.categories = (state.board.categories || []).filter(c => c.uid !== uid);
        state.board.pool = (state.board.pool || []).concat(snapshot.items);
        bumpVersion();
        if (details) details.remove();
        refreshCounts();
        offerUndo('Tier deleted — its ' + snapshot.items.length +
                  ' title(s) went back to the pool.', async () => {
            // Re-creating a tier IS a save: save_layout creates a category whose
            // uid it has not seen, and naming the same keys under it puts them
            // back in the order they were in.
            //
            // The rows have to leave the pool FIRST, in the DOM as well as in
            // state. A save that named the same title in both the pool and the
            // restored tier is refused outright, so leaving them on screen would
            // make undo fail rather than merely look wrong.
            const returning = new Set(snapshot.items);
            document.querySelectorAll('#rankerPool > .ranker-item').forEach(row => {
                if (returning.has(row.dataset.key)) row.remove();
            });
            state.board.pool = (state.board.pool || [])
                .filter(key => !returning.has(key));
            state.board.categories.push(snapshot);
            try {
                await api('/api/rankings/boards/' + encodeURIComponent(boardUid()) + '/save',
                          'POST', readLayout());
            } catch (e) { toast(e.message, false); return; }
            window.location.reload();
        });
    }, { danger: true });
}

// ---------------------------------------------------------------------------
// tiers
// ---------------------------------------------------------------------------

function newUid(prefix) {
    return prefix + '-' + Math.random().toString(36).slice(2, 10);
}

async function addTier() {
    const label = await ask({ title: 'New tier', label: 'Label', maxlength: 40 });
    if (label === null) return;
    state.board.categories.push({
        uid: newUid('tier'), label: label.slice(0, 40), rank_priority: 0,
        is_isolated: false, colour: null, items: [],
    });
    saveAndReload();
}

// THE ONE S/A/B/C/D/F SET, taken from the server rather than spelled out here.
// A board seeded from ratings already holds tiers with those exact uids, so a
// second spelling would give it two S tiers.
function applyTierTemplate() {
    const held = new Set((state.board.categories || []).map(c => c.uid));
    const missing = (state.template || []).filter(entry => !held.has(entry.uid));
    if (!missing.length) { toast('Those tiers are already on this board.', true); return; }
    missing.forEach(entry => state.board.categories.push({
        uid: entry.uid, label: entry.label, rank_priority: entry.rank_priority,
        is_isolated: false, colour: entry.colour, items: [],
    }));
    saveAndReload();
}

function openTierModal(uid) {
    const cat = tierByUid(uid);
    if (!cat) return;
    document.getElementById('tierModal').dataset.uid = uid;
    document.getElementById('tierLabel').value = cat.label || '';
    document.getElementById('tierPriority').value = cat.rank_priority;
    // Lowercased on the way in: a colour input rejects an uppercase value and
    // silently falls back to black, and the data layer stores these uppercase.
    document.getElementById('tierColour').value = (cat.colour || '#E8B545').toLowerCase();
    document.getElementById('tierIsolated').checked = !!cat.is_isolated;
    document.getElementById('tierModal').classList.add('open');
}

function closeTierModal() { document.getElementById('tierModal').classList.remove('open'); }

function saveTierSettings() {
    const modal = document.getElementById('tierModal');
    const cat = tierByUid(modal.dataset.uid);
    if (!cat) return;
    cat.label = document.getElementById('tierLabel').value.slice(0, 40);
    cat.rank_priority = parseInt(document.getElementById('tierPriority').value, 10) || 0;
    cat.colour = document.getElementById('tierColour').value.toUpperCase();
    cat.is_isolated = document.getElementById('tierIsolated').checked;
    closeTierModal();
    saveAndReload();
}

// Structural changes to the tiers themselves — created, renamed, recoloured —
// change what the server renders, so the page comes back from the server rather
// than being patched in two places that could then disagree.
async function saveAndReload() {
    setSaveState('saving');
    try {
        await api('/api/rankings/boards/' + encodeURIComponent(boardUid()) + '/save',
                  'POST', readLayout());
    } catch (e) { setSaveState('error'); toast(e.message, false); return; }
    window.location.reload();
}

// ---------------------------------------------------------------------------
// boards
// ---------------------------------------------------------------------------

async function newBoard() {
    const name = await ask({ title: 'New board', label: 'Name', maxlength: 60 });
    if (name === null) return;
    try {
        const data = await api('/api/rankings/boards', 'POST',
                               { uid: newUid('board'), name: name.slice(0, 60) });
        window.location.href = '/rankings?board=' + encodeURIComponent(data.board.uid);
    } catch (e) { toast(e.message, false); }
}

async function renameBoard() {
    const name = await ask({ title: 'Rename board', label: 'Name', maxlength: 60,
                             value: state.board.name || '', confirmText: 'Rename' });
    if (name === null) return;
    try {
        await api('/api/rankings/boards/' + encodeURIComponent(boardUid()), 'PATCH',
                  { name: name.slice(0, 60) });
        window.location.reload();
    } catch (e) { toast(e.message, false); }
}

async function cloneBoard() {
    try {
        const data = await api('/api/rankings/boards', 'POST',
                               { clone_of: boardUid(), uid: newUid('board') });
        window.location.href = '/rankings?board=' + encodeURIComponent(data.board.uid);
    } catch (e) { toast(e.message, false); }
}

function deleteBoard(event) {
    confirmInline(event.currentTarget,
        'Delete this board and everything on it? This cannot be undone.', async () => {
        try {
            await api('/api/rankings/boards/' + encodeURIComponent(boardUid()), 'DELETE', {});
        } catch (e) { toast(e.message, false); return; }
        window.location.href = '/rankings';
    }, { danger: true });
}

// ---------------------------------------------------------------------------
// search
// ---------------------------------------------------------------------------

function onSearchInput() {
    clearTimeout(searchTimer);
    // Debounced because every call reaches a provider, and the route's own
    // budget is there for a script rather than for somebody typing.
    searchTimer = setTimeout(runSearch, 350);
}

async function runSearch() {
    clearTimeout(searchTimer);
    const box = document.getElementById('searchBox');
    const results = document.getElementById('searchResults');
    const query = box.value.trim();
    if (query.length < 2) { results.hidden = true; results.innerHTML = ''; return; }
    results.hidden = false;
    results.innerHTML = '<span class="ranker-preview-hint">Searching…</span>';
    let data;
    try {
        data = await api('/api/rankings/search', 'POST',
                         { query: query, media: document.getElementById('searchMedia').value });
    } catch (e) {
        results.innerHTML = '';
        results.appendChild(Object.assign(document.createElement('span'),
            { className: 'ranker-preview-hint', textContent: e.message }));
        return;
    }
    results.innerHTML = '';
    if (!data.results.length) {
        results.innerHTML = '<span class="ranker-preview-hint">Nothing found.</span>';
        return;
    }
    data.results.forEach(ref => results.appendChild(buildResult(ref)));
    markHeldResults();
    initSearchDrag();
}

function buildResult(ref) {
    const el = document.createElement('div');
    el.className = 'ranker-result';
    el.dataset.key = ref.key;
    el.dataset.ref = JSON.stringify(ref);
    el.title = 'Drag into a tier, or click to add to the pool';
    el.onclick = () => addSearchResult(el);
    const thumb = ref.tmdb
        ? '<img loading="lazy" alt="" src="' +
          esc('/api/rankings/poster?media=' + ref.media + '&tmdb=' + ref.tmdb) +
          '" onerror="this.remove()">'
        : '';
    el.innerHTML = thumb +
        '<span class="ranker-name"><span class="ranker-title">' + esc(ref.title || 'Untitled') +
        '</span><span class="ranker-year">' +
        esc([ref.year, ref.network, ref.runtime ? ref.runtime + 'm' : ''].filter(Boolean).join(' · ')) +
        '</span></span>';
    return el;
}

// A title the board already holds is shown as held rather than hidden: seeing
// that it is already there is the answer somebody searching for it wanted.
function markHeldResults() {
    const held = new Set((state.board.pool || []).concat(
        ...(state.board.categories || []).map(c => c.items)));
    document.querySelectorAll('#searchResults .ranker-result').forEach(el => {
        el.classList.toggle('is-held', held.has(el.dataset.key));
    });
}

async function addSearchResult(el) {
    if (el.classList.contains('is-held')) return;
    const ref = JSON.parse(el.dataset.ref);
    try {
        const added = await api(
            '/api/rankings/boards/' + encodeURIComponent(boardUid()) + '/items',
            'POST', { refs: [ref] });
        if (added.added) bumpVersion();
    } catch (e) { toast(e.message, false); return; }
    state.board.pool.unshift(ref.key);
    const pool = document.getElementById('rankerPool');
    pool.insertBefore(buildRow(ref), pool.firstChild);
    const empty = document.getElementById('poolEmpty');
    if (empty) empty.remove();
    markHeldResults();
    refreshCounts();
    warmPosters([ref.key]);
}

// ---------------------------------------------------------------------------
// the optional sources
// ---------------------------------------------------------------------------

function openImportModal() {
    document.getElementById('importModal').classList.add('open');
    refreshImportYears();
}

function closeImportModal() { document.getElementById('importModal').classList.remove('open'); }

// Only the years this account actually has data for — the answer comes from the
// sources endpoint, which is the only thing that knows.
function refreshImportYears() {
    const media = document.getElementById('importMedia').value;
    const select = document.getElementById('importYear');
    const years = ((state.sources.import || {}).years || {})[media] || [];
    select.innerHTML = '<option value="">Any year</option>';
    years.forEach(year => {
        const option = document.createElement('option');
        option.value = year;
        option.textContent = year;
        select.appendChild(option);
    });
}

async function runImport() {
    const button = document.getElementById('importGo');
    button.disabled = true;
    try {
        const data = await api(
            '/api/rankings/boards/' + encodeURIComponent(boardUid()) + '/import/tracker', 'POST',
            { media: document.getElementById('importMedia').value,
              year: document.getElementById('importYear').value || null });
        toast('Found ' + data.found + ', added ' + data.added + '.', true);
        window.location.reload();
    } catch (e) {
        toast(e.message, false);
        button.disabled = false;
    }
}

async function seedFromRatings(commit) {
    try {
        const data = await api(
            '/api/rankings/boards/' + encodeURIComponent(boardUid()) + '/seed/ratings', 'POST',
            { commit: !!commit });
        if (commit) { window.location.reload(); return; }
        // The counts before anything is written — a seed rearranges a board, so
        // it says what it is about to do first.
        const ok = await ask({
            title: 'Seed from ratings',
            input: false,
            confirmText: 'Seed the board',
            message: 'This would add ' + data.titles_added + ' title(s), place ' +
                data.titles_placed + ' and create ' + data.tiers_created + ' tier(s). ' +
                data.already_placed + ' are already in a tier and will not be moved.',
        });
        if (ok) await seedFromRatings(true);
    } catch (e) { toast(e.message, false); }
}

// Posters are generated on an explicit warm rather than on the render path, so
// the client asks for the tiles it is about to show. Best-effort: a title with
// no artwork renders the placeholder and nothing here fails a page.
async function warmPosters(keys) {
    try {
        await api('/api/rankings/boards/' + encodeURIComponent(boardUid()) + '/warm',
                  'POST', { keys: keys });
    } catch (e) { /* a missing poster is a tile, not an error */ }
}

// ---------------------------------------------------------------------------
// bulk selection in the pool
// ---------------------------------------------------------------------------
// Curating a few hundred candidates one drag at a time is the difference between
// this being pleasant and being a chore.

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

// ---------------------------------------------------------------------------
// the export modal
// ---------------------------------------------------------------------------

function openExportModal() {
    const modal = document.getElementById('exportModal');
    const limits = state.limits;
    const cols = document.getElementById('exCols');
    if (!cols.options.length) {
        limits.columns.forEach(n => cols.add(new Option(n + ' columns', n)));
        cols.value = limits.columns.includes(5) ? 5 : limits.columns[0];
        const fmt = document.getElementById('exFmt');
        // Each format's ceiling is stated where it is chosen, so the hard refusal
        // is something the user was already warned about rather than a surprise.
        [['webp', 'WebP — smallest'], ['jpeg', 'JPEG — widest support'],
         ['png', 'PNG — lossless, largest']].forEach(([value, label]) => {
            const option = new Option(label, value);
            option.title = 'Maximum ' + limits.max_dimension[value].toLocaleString() +
                'px in either direction.';
            fmt.add(option);
        });
        document.getElementById('exTitle').value = defaultExportTitle();
        document.getElementById('exUser').value = state.username || '';
    }
    const scope = document.getElementById('exScope');
    scope.innerHTML = '<option value="global">All tiers, consolidated</option>';
    (state.board.categories || []).forEach(cat => {
        scope.add(new Option('Only: ' + (cat.label || 'Untitled'), 'cat:' + cat.uid));
    });
    loadHeaderImageChoices();
    modal.classList.add('open');
    onExportChange();
}

function closeExportModal() {
    document.getElementById('exportModal').classList.remove('open');
    clearTimeout(previewTimer);
}

function defaultExportTitle() {
    const scope = state.board.media_scope;
    const what = scope === 'movie' ? 'Movies' : (scope === 'show' ? 'Shows' : 'Titles');
    return 'Top ' + what + ' of ' + (state.board.year || new Date().getFullYear());
}

async function loadHeaderImageChoices() {
    const select = document.getElementById('exHeader');
    const chosen = select.value;
    select.innerHTML = '<option value="">None</option><option value="avatar">My avatar</option>';
    try {
        const data = await api('/api/me/images');
        // The account's own name for each, so this is a choice between pictures
        // rather than between positions in a list.
        data.images.forEach(image => select.add(new Option(image.name, 'img:' + image.uid)));
    } catch (e) { /* the avatar and "none" are always available */ }
    select.value = chosen;
    // An image removed from the account page is gone from here too; falling back
    // to "None" beats leaving a selection that would export as nothing.
    if (select.value !== chosen) select.value = '';
    showHeaderThumb();
}

// The picture currently chosen, beside its name. The avatar route resizes the
// master on demand, so this asks for a thumbnail rather than 512px of it.
function showHeaderThumb() {
    const value = document.getElementById('exHeader').value;
    const thumb = document.getElementById('exHeaderThumb');
    if (!value) { thumb.hidden = true; thumb.removeAttribute('src'); return; }
    thumb.src = value === 'avatar'
        ? '/api/me/avatar?size=96'
        : '/api/me/images/' + encodeURIComponent(value.slice(4));
    thumb.hidden = false;
}

// An upload goes to the account's own image store first and is then NAMED by the
// export — so the bytes are validated and normalized in exactly one place rather
// than a second time here. The name is asked for up front: an image saved with
// no name is one the picker can only call "Image 3".
async function uploadHeaderImage(input) {
    const file = input.files && input.files[0];
    if (!file) return;
    const suggested = file.name.replace(/\.[^.]+$/, '').slice(0, 40);
    const name = await ask({
        title: 'Name this image', label: 'Name', value: suggested,
        maxlength: 40, confirmText: 'Upload',
    });
    input.value = '';
    if (name === null) return;
    const reader = new FileReader();
    reader.onload = async () => {
        try {
            const data = await api('/api/me/images', 'POST',
                                   { image_b64: String(reader.result).split(',').pop(), name });
            await loadHeaderImageChoices();
            document.getElementById('exHeader').value = 'img:' + data.uid;
            onExportChange();
        } catch (e) { toast(e.message, false); }
    };
    reader.readAsDataURL(file);
}

function exportOptions() {
    const scope = document.getElementById('exScope').value;
    const header = document.getElementById('exHeader').value;
    return {
        top_x: parseInt(document.getElementById('exTopX').value, 10) || 1,
        columns: parseInt(document.getElementById('exCols').value, 10),
        title: document.getElementById('exTitle').value,
        username: document.getElementById('exUser').value,
        scope: scope.startsWith('cat:') ? 'category' : 'global',
        category_uid: scope.startsWith('cat:') ? scope.slice(4) : null,
        scale: parseFloat(document.getElementById('exScale').value),
        fmt: document.getElementById('exFmt').value,
        show_titles: document.getElementById('exTitles').checked,
        podium: document.getElementById('exPodium').checked,
        header_image: header === 'avatar' ? 'avatar'
            : (header.startsWith('img:') ? { image_uid: header.slice(4) } : null),
    };
}

// How many titles the chosen scope actually contributes — the grid is that many
// or top_x, whichever is smaller.
function scopedCount(options) {
    const cats = state.board.categories || [];
    const contributing = options.scope === 'category'
        ? cats.filter(c => c.uid === options.category_uid)
        : cats.filter(c => !c.is_isolated);
    return contributing.reduce((total, c) => total + c.items.length, 0);
}

// THE SAME ARITHMETIC compute_layout does, from the same constants — which the
// server sends rather than this file restating them, so the number shown here
// and the number the pre-render check refuses on cannot drift apart.
function computeCanvas(count, columns, scale, showTitles, podium) {
    const L = state.limits;
    const s = v => Math.max(1, Math.round(v * scale));
    const tileW = s(L.tile_w), tileH = s(L.tile_h);
    const labelH = s(L.label_h), captionH = showTitles ? s(L.caption_h) : 0;
    const gutter = Math.round(L.gutter * scale), margin = Math.round(L.margin * scale);
    const width = margin * 2 + columns * tileW + (columns - 1) * gutter;
    const contentW = width - margin * 2;
    let y = margin + s(L.header_h);
    let placed = 0;
    const podiumCount = podium ? Math.min(L.podium_ranks, count) : 0;
    if (podiumCount) {
        const podiumW = Math.floor((contentW - (podiumCount - 1) * gutter) / podiumCount);
        y += Math.round(podiumW * tileH / tileW) + labelH + captionH + gutter;
        placed += podiumCount;
    }
    const remaining = count - placed;
    const rows = remaining > 0 ? Math.ceil(remaining / columns) : 0;
    y += rows * (tileH + labelH + captionH + gutter);
    return { width: width, height: (count > 0 ? y - gutter : y) + margin };
}

function onExportChange() {
    showHeaderThumb();
    const options = exportOptions();
    const count = Math.min(options.top_x, scopedCount(options));
    const size = computeCanvas(count, options.columns, options.scale,
                               options.show_titles, options.podium);
    const limit = state.limits.max_dimension[options.fmt];
    const over = size.width > limit || size.height > limit;
    const readout = document.getElementById('exSize');
    readout.classList.toggle('is-over', over);
    readout.textContent = count
        ? (size.width + ' × ' + size.height + 'px' + (over
            ? ' — too tall for ' + options.fmt.toUpperCase() + ' (limit ' +
              limit.toLocaleString() + 'px). Try more columns, half size, or PNG.'
            : ''))
        : 'Nothing tiered to export yet.';
    document.getElementById('exportGo').disabled = over || !count;
    clearTimeout(previewTimer);
    if (!over && count) previewTimer = setTimeout(refreshPreview, 450);
}

async function refreshPreview() {
    const frame = document.getElementById('exPreview');
    try {
        const res = await fetch('/api/rankings/boards/' + encodeURIComponent(boardUid()) + '/preview', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(exportOptions()),
        });
        if (!res.ok) {
            const data = await res.json().catch(() => ({}));
            frame.innerHTML = '';
            frame.appendChild(Object.assign(document.createElement('span'),
                { className: 'ranker-preview-hint', textContent: data.error || 'No preview.' }));
            return;
        }
        const blob = await res.blob();
        // Without the revoke the previous preview's bytes stay held for the life
        // of the page, and this route is called on every option change.
        if (previewUrl) URL.revokeObjectURL(previewUrl);
        previewUrl = URL.createObjectURL(blob);
        frame.innerHTML = '';
        const img = document.createElement('img');
        img.alt = 'Preview of the poster grid';
        img.src = previewUrl;
        frame.appendChild(img);
    } catch (e) {
        frame.innerHTML = '';
        frame.appendChild(Object.assign(document.createElement('span'),
            { className: 'ranker-preview-hint', textContent: 'No preview.' }));
    }
}

async function downloadExport() {
    const button = document.getElementById('exportGo');
    // Disabled while rendering: every render takes one of two instance-wide
    // slots, and a burst of clicks would queue full-size renders behind it.
    button.disabled = true;
    button.textContent = 'Rendering…';
    try {
        const res = await fetch('/api/rankings/boards/' + encodeURIComponent(boardUid()) + '/export', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(exportOptions()),
        });
        if (!res.ok) {
            const data = await res.json().catch(() => ({}));
            toast(data.error || 'Could not export.', false);
            return;
        }
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = filenameFrom(res.headers.get('Content-Disposition')) ||
            ('ranking.' + exportOptions().fmt);
        document.body.appendChild(link);
        link.click();
        link.remove();
        // The blob is megabytes; without this it is held until the page goes.
        URL.revokeObjectURL(url);
        const mb = blob.size / (1024 * 1024);
        toast('Exported — ' + mb.toFixed(1) + ' MB.' +
              (mb > 10 ? ' Over 10 MB: try WebP or half size for Discord.' : ''), true);
    } catch (e) {
        toast('Could not export.', false);
    } finally {
        button.textContent = 'Download';
        // Through onExportChange rather than a plain re-enable, so a size that
        // the format cannot hold stays refused instead of being re-armed by the
        // act of having tried it.
        onExportChange();
    }
}

function filenameFrom(disposition) {
    const match = /filename="([^"]+)"/.exec(disposition || '');
    return match ? match[1] : null;
}

async function copyMarkdown() {
    try {
        const data = await api(
            '/api/rankings/boards/' + encodeURIComponent(boardUid()) + '/export/markdown',
            'POST', exportOptions());
        await navigator.clipboard.writeText(data.markdown);
        toast('Copied ' + data.count + ' titles as Markdown.', true);
    } catch (e) { toast(e.message || 'Could not copy.', false); }
}

// ---------------------------------------------------------------------------
// backup and restore
// ---------------------------------------------------------------------------
// Downloading is a plain link — the response is already a file with a filename
// on it. Restoring is the destructive half: it replaces every board rather than
// merging, so the file is parsed here (a broken one is caught before anything is
// sent) and the button stays disabled until the acknowledgement is typed out.

const RESTORE_ACK = 'REPLACE MY BOARDS';
let restorePayload = null;

function openBackupModal() {
    restorePayload = null;
    document.getElementById('backupFile').value = '';
    document.getElementById('backupAck').value = '';
    document.getElementById('backupConfirm').hidden = true;
    document.getElementById('backupGo').disabled = true;
    document.getElementById('backupStatus').textContent = '';
    document.getElementById('backupModal').classList.add('open');
}

function closeBackupModal() {
    document.getElementById('backupModal').classList.remove('open');
}

async function onBackupFileChosen(input) {
    const status = document.getElementById('backupStatus');
    const file = input.files && input.files[0];
    restorePayload = null;
    document.getElementById('backupConfirm').hidden = true;
    document.getElementById('backupGo').disabled = true;
    if (!file) { status.textContent = ''; return; }
    try {
        restorePayload = JSON.parse(await file.text());
    } catch (e) {
        status.textContent = 'That file is not readable as a backup.';
        return;
    }
    const boards = (restorePayload && restorePayload.boards) || [];
    status.textContent = boards.length + ' board(s) in this file.';
    document.getElementById('backupConfirm').hidden = false;
    onBackupAckInput();
}

function onBackupAckInput() {
    document.getElementById('backupGo').disabled =
        !restorePayload || document.getElementById('backupAck').value.trim() !== RESTORE_ACK;
}

async function restoreBackup() {
    const button = document.getElementById('backupGo');
    button.disabled = true;
    document.getElementById('backupStatus').textContent = 'Restoring…';
    try {
        const data = await api('/api/rankings/restore', 'POST', restorePayload);
        // A full reload rather than a redraw: every board on the page, its
        // version and its tiers have just been replaced wholesale, and the
        // server's idea of them is the only correct one now.
        toast('Restored ' + data.boards + ' board(s).', true);
        window.location.href = '/rankings';
    } catch (e) {
        document.getElementById('backupStatus').textContent = e.message || 'Could not restore.';
        onBackupAckInput();
    }
}

// ---------------------------------------------------------------------------
// wiring
// ---------------------------------------------------------------------------

function boot() {
    readPageData();
    if (!state.board) return;
    initSortable(document);
    refreshCounts();
    const pool = document.getElementById('rankerPool');
    if (pool && !pool.dataset.clickBound) {
        pool.dataset.clickBound = '1';
        pool.addEventListener('click', onPoolClick);
    }
    // The tiles for what is already tiered, warmed once on open. The pool's
    // pages warm themselves as they arrive.
    const tiered = [].concat(...(state.board.categories || []).map(c => c.items));
    if (tiered.length) warmPosters(tiered.slice(0, 250));
}

document.addEventListener('DOMContentLoaded', boot);

// A boosted board switch replaces the whole body, and a lazy fragment replaces
// one container. Both land here. Re-reading the page data covers the first;
// re-initializing covers both, and is safe when it was only a pool page that
// arrived because binding an already-bound container is a no-op.
document.addEventListener('htmx:afterSwap', (event) => {
    const swapped = (event.detail && event.detail.target) || event.target;
    // A boosted board switch brings a whole new document body, page data and
    // all; anything smaller is one container of an existing board.
    if (swapped && swapped.querySelector && swapped.querySelector('#rankerData')) {
        boot();
        return;
    }
    // Marked loaded only when rows actually arrived: a tier whose fragment
    // failed still holds its titles, and calling that container authoritative
    // would report it as empty.
    if (swapped && swapped.classList && swapped.classList.contains('ranker-rows') &&
        !swapped.querySelector('.ranker-failed')) {
        swapped.dataset.loaded = '1';
    }
    initSortable(document);
    if (state.board) {
        markHeldResults();
        renderSelection();
        refreshCounts();
        // A page of the pool that has just arrived is a page of posters nobody
        // has generated yet.
        warmPosters(keysIn(document.getElementById('rankerPool')).slice(-state.poolPageSize));
    }
});

document.addEventListener('click', (event) => {
    if (!event.target.closest('#moveMenu') && !event.target.closest('.ranker-act')) closeMoveMenu();
});

// The keys the dialog it replaces answered to, so muscle memory still works.
document.addEventListener('keydown', (event) => {
    if (!askResolve) return;
    if (event.key === 'Enter' && event.target.id === 'askInput') { event.preventDefault(); submitAsk(); }
    else if (event.key === 'Enter' && document.getElementById('askField').hidden) submitAsk();
    else if (event.key === 'Escape') closeAsk(null);
});

// A drag that has not been written yet must not leave with the tab.
window.addEventListener('beforeunload', (event) => {
    if (dirty || saving) { event.preventDefault(); event.returnValue = ''; }
});
