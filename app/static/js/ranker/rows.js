// One title on the board: drawing a row that arrives after first paint, and the
// two ways to move one that are not a drag.
//
// TAKING A TITLE OFF IS NOT HERE ANY MORE. The ✕ on a row posts itself and is
// answered with the board redrawn and a toast whose Undo is the removal history's
// own Restore button (_ranker_row.html, ranker.html), so nothing on this side
// edits the page's copy of the board, redraws the history or times an offer.

// ---------------------------------------------------------------------------
// a row that arrives after first paint
// ---------------------------------------------------------------------------
// A search result clicked or dragged in. It reaches here only AFTER the server
// holds the title, so the row is the server's as well: /rankings/fragments/row
// renders _ranker_row.html, the file the board is drawn from, and nothing on this
// side builds a row's markup. A second copy of that template used to live here,
// kept in step by hand.
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
// FETCHED BY htmx RATHER THAN BY HAND, and its own comment used to argue the
// opposite. The argument was that nothing has to run after the row lands and that
// htmx's after-swap event would name a whitespace text node rather than the row —
// both still true, and neither is a reason to write the fetch, the parse and the
// replace here. htmx.ajax with an element target and an outerHTML swap is that
// replace; an empty answer (another tab took the title off while this was in
// flight) removes the placeholder, which is the right row to draw for it.
function insertRow(container, before, key) {
    const slot = document.createElement('div');
    slot.className = 'ranker-item is-arriving';
    slot.setAttribute('role', 'listitem');
    slot.dataset.key = key;
    container.insertBefore(slot, before || null);
    htmx.ajax('GET', '/rankings/fragments/row?board=' + encodeURIComponent(boardUid()) +
                     '&key=' + encodeURIComponent(key),
              { target: slot, swap: 'outerHTML' });
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

// THE MENU'S CHOICES ARE MARKUP (ranker.html renders one per tier) AND THE MENU
// IS A POPOVER. Opening it puts it in the top layer and gives it light-dismiss
// and Escape for nothing, which is what deleted the document-wide click listener
// that used to close it. What stays here is what markup cannot know: which row it
// was opened from, and where on the screen that button is — a measurement of
// rendered geometry.
let movingRow = null;

function openMoveMenu(button) {
    movingRow = button.closest('.ranker-item');
    const menu = document.getElementById('moveMenu');
    // SHOWN BEFORE IT IS PLACED, because a popover has no box until it is open
    // and `offsetWidth` would read zero. In the top layer it is positioned
    // against the viewport, so the button's own viewport rectangle is the whole
    // measurement — the page's scroll offset used to have to be added back.
    menu.showPopover();
    const box = button.getBoundingClientRect();
    menu.style.top = (box.bottom + 4) + 'px';
    menu.style.left = Math.max(8, box.right - menu.offsetWidth) + 'px';
}

async function moveRowTo(row, categoryUid) {
    const container = categoryUid === null
        ? document.getElementById('rankerPool')
        : await openTierBody(categoryUid);
    if (!container) return;
    if (categoryUid !== null) unpick(row);
    container.appendChild(row);
    refreshCounts();
    scheduleSave();
}

// Opens a tier and makes sure its rows are actually on the page, so a title
// moved into a tier nobody has opened lands somewhere real rather than into a
// container whose contents the client has never seen.
//
// IT REUSES THE DISCLOSURE'S OWN REQUEST rather than fetching the same address a
// second way: the address is on the summary, as markup, and htmx.ajax into the
// same body is the same swap opening the tier would have done — which means
// ranker/boot.js marks it loaded and binds it for dragging exactly as it does for
// a tier opened by hand, and this has none of that to repeat.
async function openTierBody(categoryUid) {
    const details = document.querySelector('.ranker-tier[data-uid="' + CSS.escape(categoryUid) + '"]');
    const body = document.getElementById('tierBody-' + categoryUid);
    if (!body) return null;
    if (details) details.open = true;
    if (body.dataset.loaded === '1') return body;
    const url = details && details.querySelector('summary').getAttribute('hx-get');
    if (!url) return null;
    await htmx.ajax('GET', url, { target: body, swap: 'innerHTML' });
    if (body.dataset.loaded !== '1') { toast('Could not open that tier.', false); return null; }
    return body;
}

document.addEventListener('click', (event) => {
    const choice = event.target.closest('#moveMenu [data-move-to]');
    if (!choice) return;
    const row = movingRow;
    document.getElementById('moveMenu').hidePopover();
    movingRow = null;
    // An empty choice is the pool, which moveRowTo names as null.
    if (row) moveRowTo(row, choice.dataset.moveTo || null);
});
