// Writing the board back: reading the current arrangement out of the DOM,
// coalescing a run of drags into one request, the two ways a save ends, and the
// one hook that makes every other action on this page safe to press mid-drag.

let saveTimer = null;
let inFlight = null;            // the write currently on the wire, so a flush can wait for it
let dirty = false;

// Coalesces a run of quick drags into one write, and is also the window in which
// a pending arrangement can be lost — see the flush at the bottom of this file.
const SAVE_DELAY_MS = 800;

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

function scheduleSave() {
    dirty = true;
    setSaveState('saving');
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => saveNow(), SAVE_DELAY_MS);
}

// Writes the arrangement if there is one to write, waiting for any write already
// on the wire. ANSWERS WHETHER THE SERVER NOW HOLDS WHAT THIS PAGE SHOWS, which
// is what the flush below needs: a 409 or a refusal answers false, and the caller
// decides what that means for whatever it was about to do.
async function saveNow() {
    if (!state.board) return true;
    while (inFlight) await inFlight;     // writeLayout never rejects
    if (!dirty) return true;             // somebody else's flush already wrote it
    clearTimeout(saveTimer);
    dirty = false;
    inFlight = writeLayout();
    try {
        return await inFlight;
    } finally {
        inFlight = null;
    }
}

async function writeLayout() {
    try {
        const data = await api('/api/rankings/boards/' + encodeURIComponent(boardUid()) + '/save',
                               'POST', readLayout());
        state.board.version = data.version;
        setSaveState(dirty ? 'saving' : 'saved');
        // THE TIER HEADERS' ARTWORK IS THE SERVER'S VIEW OF WHAT EACH TIER HOLDS,
        // and a drag, a nudge or the move menu has just changed that — so the
        // strip drawn with the page is now describing an arrangement that no
        // longer exists. Every other action on this page answers with the board
        // and redraws them; these end in a save instead, so the save says the
        // tiers moved and the markup beside them owns the address
        // (_ranker_board_shell.html). Nothing on this side draws a strip.
        htmx.trigger('#tierStripsRefresh', 'tiers-changed');
        return true;
    } catch (e) {
        if (e.status === 409) {
            // Another tab saved first. Reloading is the honest answer: guessing
            // which arrangement wins would silently discard somebody's work.
            setSaveState('error');
            toast('This board changed in another tab — reloading it.', false);
            setTimeout(() => window.location.reload(), 1200);
            return false;
        }
        setSaveState('error');
        toast(e.message, false);
        return false;
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
// a pending drag, and everything else on this page
// ---------------------------------------------------------------------------
// EVERY ACTION HERE THAT IS NOT A DRAG NOW ANSWERS WITH THE BOARD REDRAWN, which
// replaces the DOM the next save would have read its arrangement out of. A drag
// made in the last SAVE_DELAY_MS and not yet written would simply be gone. So the
// pending write is flushed FIRST, once, stated here rather than remembered at
// each of the dozen controls that would otherwise have to.
//
// HOW A REQUEST IS HELD, READ IN THE VENDORED htmx RATHER THAN ASSUMED. The event
// cannot do it: `htmx:before:request` is dispatched synchronously and its only
// power is to CANCEL, with no way to re-issue what it cancelled — a listener that
// returned a promise would be ignored. What can is the context's own `fetch`:
// htmx sets `ctx.fetch ||= window.fetch.bind(window)` immediately BEFORE that
// event and then calls `await ctx.fetch(...)`, reading the property at call time.
// Wrapping it therefore holds the request for exactly as long as the save takes,
// with htmx's own indicators and abort signal still in place.
//
// A FAILED SAVE ABORTS THE REQUEST. Throwing from the wrapper leaves htmx in its
// catch with no swap at all, so a 409 gets the reload it asked for instead of a
// freshly-drawn board landing on top of an arrangement the server has rejected.
//
// GETs ARE LET THROUGH. A read changes nothing this page would have to save
// first, and holding the pool's paging sentinel — which fires while a drag is in
// progress, because dragging scrolls it into view — behind a write would be the
// one place this hook could make things worse.
document.addEventListener('htmx:before:request', (event) => {
    const ctx = event.detail && event.detail.ctx;
    // The board on screen, not the board this file last read: these scripts stay
    // resident across a boosted navigation, so on any other page there is nothing
    // here to save.
    if (!ctx || ctx.request.method === 'GET') return;
    if (!state.board || !document.getElementById('rankerBoard')) return;
    const send = ctx.fetch;
    ctx.fetch = async (...args) => {
        if (!await saveNow()) throw new Error('The board could not be saved first.');
        return send(...args);
    };
});

// A drag that has not been written yet must not leave with the tab.
window.addEventListener('beforeunload', (event) => {
    if (dirty || inFlight) { event.preventDefault(); event.returnValue = ''; }
});
