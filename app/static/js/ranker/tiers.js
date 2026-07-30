// ---------------------------------------------------------------------------
// tiers
// ---------------------------------------------------------------------------

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
