// One title on the board: drawing a row that arrives after first paint, keeping a
// page of the pool from drawing a title twice, and the two ways to move one that
// are not a drag.
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
// a page of the pool never draws a title this page already shows
// ---------------------------------------------------------------------------
// The pool arrives a page at a time, each page "the titles after the one the last
// page ended on" (ranker.fetch_pool_after), so a page cannot skip anything. What it
// CAN do is repeat: a title dragged back into the pool from a tier on this page,
// or one just added and drawn at the head of the pool, is also somewhere in the
// server's pool after that point. Drawn twice, the next save names it twice and
// is refused whole — MEASURED, and every save after it failed the same way until
// a reload, which then lost the moves that had not been written.
//
// WHY THIS IS SCRIPT: until a save lands, THIS PAGE is the arrangement. No route
// can know what has been dragged here and not yet written, and no template can
// leave out a row it does not know is already drawn — only the page can. The row
// the page already has is kept, because it is where somebody put it; the arriving
// copy is dropped before it is inserted.
//
// READ IN THE VENDORED htmx RATHER THAN ASSUMED: `htmx:before:swap` carries the
// swap's tasks before any runs, and a pool page's rows are an out-of-band task
// targeting #rankerPool whose `fragment` is what will be appended.
document.addEventListener('htmx:before:swap', (event) => {
    const pool = document.getElementById('rankerPool');
    const tasks = (event.detail && event.detail.tasks) || [];
    const pages = tasks.filter(task => pool && task.target === pool && task.fragment);
    if (!pages.length) return;
    const shown = new Set(Array.from(document.querySelectorAll('.ranker-item[data-key]'),
                                     row => row.dataset.key));
    pages.forEach(task => task.fragment.querySelectorAll('.ranker-item[data-key]').forEach(row => {
        if (shown.has(row.dataset.key)) row.remove();
    }));
});

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

// MOVING A TITLE IS NOT HERE ANY MORE. Each row carries its own menu of places
// to go and each choice posts itself to the route the bulk Move already used
// (_ranker_row.html), answered with the board redrawn. What went with it was all
// bookkeeping for one shared menu: which ⇄ had opened it, walking the row into
// the chosen container, and scheduling the save afterwards. A pending drag is
// still written first, and nothing had to be added for that — the move is a
// POST, and ranker/save.js holds every non-GET behind the save already.
