// The rankings page's wiring: one idempotent boot, and the swaps that have to
// re-run it.
//
// WHY THIS IS SCRIPT AT ALL, since every action on this page is markup that
// posts itself and is answered with the board redrawn: what it re-runs is the
// DRAG BINDING, and a drop target is something only the pointer machinery can be
// given (ranker/dnd.js). A fragment arriving — a tier's rows, a page of the
// pool — brings new containers with it, and the server has no way to say "and
// bind these": the markup it sends cannot hand an element to a library already
// running in the page. Everything else this file does is bookkeeping for that
// one act, and the counts beside it, which are read off containers the DOM is
// authoritative for mid-drag.
//
// NOTHING RUNS AS THIS FILE LOADS. It declares boot() and listens for the moment
// to run it, so it depends on no other file having run first.

function boot() {
    readPageData();
    if (!state.board) return;
    initSortable(document);
    refreshCounts();
}

// Hung off `page-arrived` rather than DOMContentLoaded, which a boosted arrival
// does not fire. static/js/boost.js announces it on the cold load, on a boosted
// board switch, and on arriving here from any other page, which are the same
// thing as far as this page is concerned. Every action on this page that is not a
// drag answers with the board redrawn, which arrives the same way and re-runs
// this. Every page hears it, so this runs only for the one naming itself.
document.addEventListener('page-arrived', (event) => {
    if (event.detail.page === 'rankings') boot();
});

// What is left here is the LAZY FRAGMENT: a tier's body, a page of the pool or a
// single row replacing one container. Re-initializing is safe because binding an
// already-bound container is a no-op.
document.addEventListener('htmx:after:swap', (event) => {
    // ONLY WHILE A BOARD IS ON SCREEN. This file runs on every page, and every
    // page swaps fragments in — a calendar day block, a details panel — while the
    // CALENDAR carries a `#searchResults` of its own, which the binding below
    // would take for this board's list and hand to the drag library.
    if (!document.getElementById('rankerBoard')) return;
    // static/js/boost.js's swappedContent, so "which element did this swap
    // produce" is answered once for the tier body (which keeps its container)
    // and the pool sentinel (which replaces itself) alike. Null is a whole-page
    // arrival, which boot() above already handles.
    const swapped = swappedContent(event);
    if (!swapped) return;
    // THE DETAILS PANEL, THE TIER SETTINGS DIALOG AND THE EXPORT READOUT ARE NOT
    // THE BOARD. Each swaps content in beside a board that is already bound and
    // counted, and none of them brings a row, so rebinding and recounting on every
    // panel opened and every option changed would be work for nothing.
    if (swapped.closest && swapped.closest('#detailsModal, #tierSettings, #exportReadout')) return;
    // NOR ARE THE SEARCH RESULTS. They arrive already saying which titles the
    // board holds, and a result is not a row — nothing there is a drop target or
    // a thing to count. The list Sortable clones FROM is bound by initSortable
    // below on any swap that does bring rows, and #searchResults keeps its
    // element across one of these, so the binding it already has still holds.
    if (swapped.closest && swapped.closest('#searchResults')) return;
    // Marked loaded only when rows actually arrived: a tier whose fragment
    // failed still holds its titles, and calling that container authoritative
    // would report it as empty.
    if (swapped.classList && swapped.classList.contains('ranker-rows') &&
        !swapped.querySelector('.ranker-failed')) {
        swapped.dataset.loaded = '1';
        // AND THE DISCLOSURE STOPS ASKING FOR THEM. Its `click once` has not
        // fired when the rows were fetched some other way — a title moved into a
        // tier nobody had opened — and firing later would swap the server's rows
        // over an arrangement this page has since changed.
        const head = swapped.parentElement && swapped.parentElement.querySelector('summary');
        if (head) head.removeAttribute('hx-get');
    }
    initSortable(document);
    if (state.board) refreshCounts();
});
