// DRAGGING A TITLE, WHICH IS A STREAM OF POINTER EVENTS AND SO CANNOT BE
// ANYTHING ELSE. There is no markup for "follow the pointer", no stylesheet rule
// that reorders a list as a finger moves over it, and no request that could be
// made until the drop decides where the title went — the server is told
// afterwards, by the save (ranker/save.js).
//
// SORTABLE DOES THE POINTER WORK AND THIS FILE DOES THE BINDING. What is here is
// which containers are drop targets and when they become one, which is a
// question about markup that has arrived: htmx replaces the containers as tiers
// open and pages of the pool land, and binding one twice produces two drop
// handlers and therefore two saves of one drag.
//
// EVERY REORDER A DRAG CAN DO IS ALSO REACHABLE WITHOUT ONE — the ▲▼ buttons
// and the move menu on each row (ranker/rows.js) — which is what keeps the
// vendored library a convenience rather than something the feature depends on.

const sortables = new Map();    // element -> Sortable instance, so a detached one can be destroyed

function keysIn(container) {
    if (!container) return [];
    return Array.from(container.querySelectorAll(':scope > .ranker-item'))
        .map(el => el.dataset.key);
}

// Every container that holds draggable titles: the pool, and each tier body THAT
// HAS ITS ROWS. Search results are bound separately — they clone rather than
// move.
//
// `[data-loaded]` is not a nicety. Above the row threshold a board draws its
// tiers closed and each fetches its rows the first time it is opened, so an
// unopened tier's body sits in the document with no children — and Sortable
// treats a childless drop target as a place to insert, scanning every one of
// them against the pointer on every move (its emptyInsertThreshold) and handing
// the drag over to any it lands inside. A tier nobody has opened is not
// somewhere a title can be dropped: it is not even on screen. Binding it made
// dragging into the tier BELOW it shudder, the displaced row shuffling aside
// over and over as the drag was taken away and given back. ranker/boot.js marks
// each body loaded as its fragment lands and re-runs the binding, so a tier
// becomes a drop target exactly when it has somewhere to drop into. A body that
// is loaded and genuinely empty stays a target, which is how a new tier gets its
// first title.
const DROP_TARGETS = '.ranker-pool, .ranker-rows[data-loaded]';

function dropTargets(root) {
    const scope = root && root.querySelectorAll ? root : document;
    const found = Array.from(scope.querySelectorAll(DROP_TARGETS));
    if (scope.matches && scope.matches(DROP_TARGETS)) found.push(scope);
    return found;
}

// SAFE TO CALL ON ANY SUBTREE, ANY NUMBER OF TIMES. A container that is already
// bound is skipped, so the load-time call and the per-swap call cannot between
// them install two sets of handlers on the same element.
//
// DESTROYING A DETACHED INSTANCE IS ALSO WHAT EMPTIES THE SELECTION after the
// board is redrawn. MultiDrag holds the picked rows in one array shared by every
// container, so after a redraw it would be holding rows no longer in the page —
// and a drag carries every row in it. MEASURED in Firefox 155 and Chromium 153:
// destroying the instance the last pick was made in deselects every row it held,
// detached or not, and the next drag carries only what is on screen.
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
            // Also what keeps ✕, ⓘ, the nudges and ⇄ from picking their row: a
            // press the library filters out never reaches its selection either.
            filter: '.ranker-act',
            preventOnFilter: false,
            ghostClass: 'sortable-ghost',
            dragClass: 'sortable-drag',
            // One selection across the pool and every open tier (ranker/selection.js).
            // `avoidImplicitDeselect` because the library otherwise drops the
            // whole selection on any click outside the container it was made in —
            // which is every pick in a second column, and the Move button itself.
            multiDrag: true,
            selectedClass: 'sortable-selected',
            avoidImplicitDeselect: true,
            onSelect: onPicked,
            onDeselect: onUnpicked,
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
        // A result with no key names no row yet (the add has to look it up), so
        // there is nothing to insert where it drops; it is added by clicking.
        draggable: '.ranker-hit[data-key]:not(.is-held)',
        animation: 140,
        onEnd: onSearchDragEnd,
    }));
}

// One row put back where it was changed nothing. A drag carrying a selection is
// saved whatever its own row did, because the others moved even when it did not
// — and what moved is read back out of the DOM by the save (ranker/save.js's
// readLayout), which is why nothing here has to know how many rows went where.
function onDragEnd(evt) {
    if (evt.items.length < 2 && evt.from === evt.to && evt.oldIndex === evt.newIndex) return;
    refreshCounts();
    scheduleSave();
}

// A search result dropped straight into a tier: add it to the board first, then
// draw the server's row where the clone was dropped (rows.js's insertRow), so it
// is a title like any other.
//
// THE CLONE COMES OUT BEFORE THE ADD, and the row's placeholder goes in only once
// the add has succeeded. Turning the clone into the placeholder straight away
// would put the key in the DOM while the board does not hold the title yet, and a
// save firing in that gap would be refused whole — save_layout rejects a layout
// over one key it does not know.
async function onSearchDragEnd(evt) {
    const dropped = evt.item;
    if (!dropped || !dropped.parentElement || dropped.parentElement.id === 'searchResults') return;
    const key = dropped.dataset.key;
    const target = dropped.parentElement;
    const index = Array.from(target.children).indexOf(dropped);
    dropped.remove();
    // The clone still carries what the result did — the reference it was drawn
    // with — which is all an add sends (search.js's addFoundTitle).
    let added;
    try {
        added = await addFoundTitle(dropped);
    } catch (e) {
        toast(e.message, false);
        return;
    }
    insertRow(target, target.children[index], added || key);
    // THE ONE RESULT THIS JUST ADDED, and only that one. The list is the server's
    // and arrives with the board's holdings already marked
    // (_ranker_search_results.html); a drop is the single way of adding that does
    // not redraw it, so what is marked here is a fact this side has and the
    // drawn list could not have had. The clone was removed above — the original
    // is still in the list and is what has to stop being draggable.
    const offered = document.querySelector(
        '#searchResults .ranker-hit[data-key="' + CSS.escape(key) + '"]');
    if (offered) offered.classList.add('is-held');
    refreshCounts();
    scheduleSave();
}
