// ---------------------------------------------------------------------------
// deleting a tier, and taking it back
// ---------------------------------------------------------------------------
// EVERY OTHER TIER ACTION IS MARKUP NOW. Adding one, applying the template and
// writing a tier's settings are forms that post themselves and are answered with
// the board redrawn (_ranker_board_shell.html, _ranker_tier_settings.html), so
// what a tier may be called, which uid a new one gets and what the template means
// are all the server's.
//
// THIS ONE IS NOT, AND IT IS WAITING ON SOMETHING RATHER THAN DEFENDED. A deleted
// tier's undo is the only undo in this app still held in the browser: it keeps a
// snapshot of the tier, re-creates it through a layout save and reloads, so it is
// one level deep, gone on reload and invisible to a second tab. Titles removed
// from a board already have the lasting kind — a history on the server, offered
// after a reload and from another tab — and a tier needs a record of its own
// before it can have the same. Until then the snapshot stays, because what it
// does for the person who mis-clicked is real.

let undoAction = null;          // the toast's one pending action

// A moment's offer tied to the click that made it, with no state of its own on
// the server to be a fragment of. The removal toast that used to share this is
// the server's now (ranker.html): it draws a toast whose Undo is the removal
// history's own Restore button. This is the one caller left.
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

// ASKING IS THE MARKUP'S (templates/_ranker_board_shell.html draws the question
// with templates/_confirm.html); this is only what happens after Delete is
// pressed. It used to open a hand-placed popover, and it was the last caller of
// that helper in the app.
async function deleteTier(uid) {
    const cat = tierByUid(uid);
    if (!cat) return;
    const details = document.querySelector('.ranker-tier[data-uid="' + CSS.escape(uid) + '"]');
    const snapshot = Object.assign({}, cat, { items: cat.items.slice() });
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
    // artwork is already loaded and nothing has to be drawn again.
    const pool = document.getElementById('rankerPool');
    if (body && pool) {
        Array.from(body.querySelectorAll(':scope > .ranker-item'))
            .forEach(row => pool.appendChild(row));
    }
    const empty = document.getElementById('poolEmpty');
    if (empty) empty.remove();
    state.board.categories = (state.board.categories || []).filter(c => c.uid !== uid);
    // The move menu, the bulk-move list and the export dialog's scope are
    // server markup naming every tier. A delete is the one tier change that
    // does not redraw the page, so its choices are taken out here.
    document.querySelectorAll('[data-tier-choice="' + CSS.escape(uid) + '"]')
        .forEach(choice => choice.remove());
    state.board.pool = (state.board.pool || []).concat(snapshot.items);
    bumpVersion();
    if (details) details.closest('.ranker-tier-shell').remove();
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
}
