// ---------------------------------------------------------------------------
// drag and drop
// ---------------------------------------------------------------------------

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
    // The clone still carries what the result did — its key and its query — which
    // is all an add sends (search.js's addFoundTitle).
    try {
        await addFoundTitle(dropped);
    } catch (e) {
        toast(e.message, false);
        return;
    }
    insertRow(target, target.children[index], key);
    markHeldResults();
    refreshCounts();
    scheduleSave();
}
