// ---------------------------------------------------------------------------
// search
// ---------------------------------------------------------------------------
// THE SEARCH ITSELF IS MARKUP. The box asks /rankings/fragments/search as it is
// typed into and the server draws the results (_ranker_board_shell.html,
// _ranker_search_results.html); the debounce, the request and the rendering that
// used to live here are gone. What is left is what markup cannot do: adding a
// result, which changes the board and so has to move the page's copy of it, and
// marking the results the board already holds, which changes without a request.

// A title the board already holds is shown as held rather than hidden: seeing
// that it is already there is the answer somebody searching for it wanted.
function markHeldResults() {
    const held = new Set((state.board.pool || []).concat(
        ...(state.board.categories || []).map(c => c.items)));
    document.querySelectorAll('#searchResults .ranker-result').forEach(el => {
        el.classList.toggle('is-held', held.has(el.dataset.key));
    });
}

// ADDING A RESULT NAMES IT; IT DOES NOT HAND OVER A TITLE. The result carries its
// key and the query that found it, and the server reads the title back out of
// that search's stored answer — so what is stored is what the service said, and
// the add costs no second lookup. Shared by a click and by a drop (dnd.js).
async function addFoundTitle(result) {
    const added = await api(
        '/api/rankings/boards/' + encodeURIComponent(boardUid()) + '/items',
        'POST', { query: result.dataset.query, key: result.dataset.key });
    if (added.added) bumpVersion();
}

async function addSearchResult(el) {
    if (el.classList.contains('is-held')) return;
    const key = el.dataset.key;
    try { await addFoundTitle(el); } catch (e) { toast(e.message, false); return; }
    state.board.pool.unshift(key);
    const pool = document.getElementById('rankerPool');
    insertRow(pool, pool.firstChild, key);
    const empty = document.getElementById('poolEmpty');
    if (empty) empty.remove();
    markHeldResults();
    refreshCounts();
}

document.addEventListener('click', (event) => {
    const result = event.target.closest('#searchResults .ranker-result');
    if (result) addSearchResult(result);
});
