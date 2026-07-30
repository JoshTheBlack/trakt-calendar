// Writing the board back: reading the current arrangement out of the DOM,
// coalescing a run of drags into one request, and the two ways a save ends.

let saveTimer = null;
let saving = false;
let dirty = false;

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

// A drag that has not been written yet must not leave with the tab.
window.addEventListener('beforeunload', (event) => {
    if (dirty || saving) { event.preventDefault(); event.returnValue = ''; }
});
