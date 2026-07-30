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

document.addEventListener('DOMContentLoaded', boot);

// A boosted board switch replaces the whole body, and a lazy fragment replaces
// one container. Both land here. Re-reading the page data covers the first;
// re-initializing covers both, and is safe when it was only a pool page that
// arrived because binding an already-bound container is a no-op.
document.addEventListener('htmx:afterSwap', (event) => {
    const swapped = (event.detail && event.detail.target) || event.target;
    // A boosted board switch brings a whole new document body, page data and
    // all; anything smaller is one container of an existing board.
    if (swapped && swapped.querySelector && swapped.querySelector('#rankerData')) {
        boot();
        return;
    }
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
