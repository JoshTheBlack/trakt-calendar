// ---------------------------------------------------------------------------
// adding a title a search found
// ---------------------------------------------------------------------------
// THE SEARCH IS MARKUP, AND SO IS ADDING BY CLICKING. The box asks
// /rankings/fragments/search as it is typed into and the server draws the
// results; a result posts itself and is answered with the page, carrying that
// same search redrawn from the cache its own request filled
// (_ranker_search_results.html). Whether a result is already held is drawn there
// too — the sweep that used to write it on afterwards is gone, and with it the
// page's habit of deciding that against a copy of the board it only ever bumps.
//
// WHAT IS LEFT IS THE ONE ADD A PAGE CANNOT ANSWER: a result DRAGGED straight
// into a tier. The row has to land where the pointer dropped it — a position
// among siblings no swap can name — so this path takes the count back and puts
// the row there itself (ranker/dnd.js). It names the result rather than handing
// over a title: the key and the query that found it go back, and the server
// reads the title out of that search's own stored answer, so what is stored is
// what the service said and the add costs no second lookup.

async function addFoundTitle(result) {
    const added = await api(
        '/api/rankings/boards/' + encodeURIComponent(boardUid()) + '/items',
        'POST', { query: result.dataset.query, key: result.dataset.key });
    if (added.added) bumpVersion();
}
