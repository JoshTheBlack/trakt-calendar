// The rankings page's wiring: one idempotent boot, and the swaps that have to
// re-run it.
//
// LOADED LAST of the page's scripts, deliberately. These files are ordinary
// scripts sharing one global scope, executed in the order the <head> lists them
// (defer preserves it), so everything boot() calls is declared before it runs.

function boot() {
    readPageData();
    if (!state.board) return;
    initSortable(document);
    refreshCounts();
    const pool = document.getElementById('rankerPool');
    if (pool && !pool.dataset.clickBound) {
        pool.dataset.clickBound = '1';
        pool.addEventListener('click', onPoolClick);
    }
    // The tiles for what is already tiered, warmed once on open. The pool's
    // pages warm themselves as they arrive.
    const tiered = [].concat(...(state.board.categories || []).map(c => c.items));
    if (tiered.length) warmPosters(tiered.slice(0, 250));
}

// Registered rather than hung off DOMContentLoaded, which a boosted arrival does
// not fire. static/js/boost.js calls it once this page's scripts are loaded — on
// the cold load, on a boosted board switch, and on arriving here from any other
// page, which are the same thing as far as this page is concerned.
registerPage('rankings', boot);

// What is left here is the LAZY FRAGMENT: a tier's body or a page of the pool
// replacing one container. Re-initializing is safe because binding an
// already-bound container is a no-op.
document.addEventListener('htmx:afterSwap', (event) => {
    if (event.detail && event.detail.boosted) return;
    const swapped = (event.detail && event.detail.target) || event.target;
    // Marked loaded only when rows actually arrived: a tier whose fragment
    // failed still holds its titles, and calling that container authoritative
    // would report it as empty.
    if (swapped && swapped.classList && swapped.classList.contains('ranker-rows') &&
        !swapped.querySelector('.ranker-failed')) {
        swapped.dataset.loaded = '1';
    }
    initSortable(document);
    if (state.board) {
        markHeldResults();
        renderSelection();
        refreshCounts();
        // A page of the pool that has just arrived is a page of posters nobody
        // has generated yet.
        warmPosters(keysIn(document.getElementById('rankerPool')).slice(-state.poolPageSize));
    }
});
