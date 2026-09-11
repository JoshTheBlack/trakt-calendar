// One title on the board: drawing a row that arrives after first paint, every way
// to move or remove one that is not a drag, and putting a removed one back.

let undoAction = null;          // the toast's one pending action — see UNDO below

// ---------------------------------------------------------------------------
// a row that arrives after first paint
// ---------------------------------------------------------------------------
// A search result clicked or dragged in. It reaches here only AFTER the server
// holds the title, so the row is the server's as well: /rankings/fragments/row renders _ranker_row.html, the file the
// board is drawn from, and nothing on this side builds a row's markup. A second
// copy of that template used to live here, kept in step by hand.
//
// WHAT HAS TO BE SCRIPT IS WHERE IT GOES. The position is among siblings the drag
// library owns, chosen by a drop, and only this side knows which element the row
// belongs next to.
//
// A KEYED PLACEHOLDER GOES IN FIRST, which is what lets the fetch be slow. The
// layout is read out of the DOM at save time (core.js, rule 3), so a save that
// fires while the row is on its way still names the title, in the right place,
// because the placeholder already carries the key. A row inserted only once the
// fragment landed would be missing from any save that beat it there.
//
// FETCHED AND PARSED HERE RATHER THAN SWAPPED IN BY htmx, and that is measured
// rather than assumed. When an outerHTML swap replaces the element it was aimed
// at, htmx re-points the request's source at the FIRST NODE it inserted and
// dispatches its after-swap event there — and every fragment here begins with a
// newline, so that node is a whitespace text node. boost.js's swappedContent then
// answers with that text node rather than with the row. Nothing would say which
// row landed, and nothing needs to: its key, its position and every count were
// already right when the placeholder went in, so one synchronous replace with no
// event to interpret is all this takes.
function insertRow(container, before, key) {
    const slot = document.createElement('div');
    slot.className = 'ranker-item is-arriving';
    slot.setAttribute('role', 'listitem');
    slot.dataset.key = key;
    container.insertBefore(slot, before || null);
    fetch('/rankings/fragments/row?board=' + encodeURIComponent(boardUid()) +
          '&key=' + encodeURIComponent(key))
        .then(res => (res.ok ? res.text() : Promise.reject(new Error(String(res.status)))))
        .then(html => {
            const parsed = document.createElement('template');
            parsed.innerHTML = html;
            const row = parsed.content.querySelector('.ranker-item');
            // An empty answer is a title another tab took off the board while this
            // was in flight, and no row is the right thing to draw for it.
            if (row) slot.replaceWith(row); else slot.remove();
        })
        .catch(() => {
            // Left where it is: it still carries the key, so a save stays right,
            // and a reload draws the row the server already holds.
        });
    return slot;
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

// THE MENU'S CHOICES ARE MARKUP (ranker.html renders one per tier). What stays
// here is what markup cannot know: which row it was opened from, and where on
// the screen that button is — a measurement of rendered geometry.
let movingRow = null;

function openMoveMenu(button) {
    movingRow = button.closest('.ranker-item');
    const menu = document.getElementById('moveMenu');
    const box = button.getBoundingClientRect();
    menu.hidden = false;
    menu.style.top = (window.scrollY + box.bottom + 4) + 'px';
    menu.style.left = Math.max(8, window.scrollX + box.right - menu.offsetWidth) + 'px';
}

function closeMoveMenu() {
    const menu = document.getElementById('moveMenu');
    if (menu) menu.hidden = true;
    movingRow = null;
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
// reverse is taking a title off the board and deleting a tier, so both offer an
// Undo in a toast the moment they happen.
//
// THE TWO UNDOS ARE NOT THE SAME KIND. A deleted tier's titles are still on the
// board — they went to the pool — so undoing it is a re-save of what this page
// already holds (tiers.js): one level, gone on reload. A removed title has left
// the board, so undoing that is the server's: the board keeps a history of what
// was taken off it (ranker/core.py), and the "Recently removed" list offers it —
// after a reload, from another tab, or tomorrow. That list, and restoring from
// it, is entirely markup (_ranker_removed.html).
//
// WHY THE TOAST STAYS SCRIPT: it is a moment's offer tied to the click that made
// it, with no state of its own on the server to be a fragment of. Its Undo does
// not restore anything itself — it presses the list's own Restore button.

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
        markHeldResults();
        refreshCounts();
        removalsChanged();
        // PRESSES THE LIST'S OWN Restore BUTTON, so a title comes back one way
        // however it is asked for, and that way is markup. The list is redrawn the
        // moment the removal lands, long before anybody can reach this button;
        // should it somehow not be there, the list is where to look.
        offerUndo('Removed from the board.', () => {
            const restore = document.querySelector(
                '#rankerRemoved [data-restore-key="' + CSS.escape(key) + '"]');
            if (restore) restore.click();
            else toast('It is under Recently removed.', true);
        });
    }, { danger: true });
}

// THE HISTORY REDRAWS ITSELF. The element beside it carries its address and
// listens for this event (_ranker_board_shell.html), so all this side says is
// that something changed — not where the list lives or how it is drawn.
function removalsChanged() {
    htmx.trigger('#rankerRemovedRefresh', 'removals-changed');
}

document.addEventListener('click', (event) => {
    const choice = event.target.closest('#moveMenu [data-move-to]');
    if (choice) {
        const row = movingRow;
        closeMoveMenu();
        // An empty choice is the pool, which moveRowTo names as null.
        if (row) moveRowTo(row, choice.dataset.moveTo || null);
        return;
    }
    if (!event.target.closest('#moveMenu') && !event.target.closest('.ranker-act')) closeMoveMenu();
});
