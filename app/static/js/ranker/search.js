// ---------------------------------------------------------------------------
// adding a title a search found
// ---------------------------------------------------------------------------
// THE SEARCH IS MARKUP, AND SO IS ADDING BY CLICKING. The box is the app's one
// title search (_title_search.html) and the server draws the results; a result
// posts itself and is answered with the page, carrying that same search redrawn
// from what its own request stored (_ranker_search_results.html). Whether a
// result is already held is drawn there too, and a show's row opens its season
// checklist under it.
//
// WHAT IS LEFT IS THE ONE ADD A PAGE CANNOT ANSWER: a result DRAGGED straight
// into a tier. The row has to land where the pointer dropped it — a position
// among siblings no swap can name — so this path takes the row's key back and
// puts the row there itself (ranker/dnd.js). It names the result exactly as a
// click does, by the reference the server drew on the result — every data-
// attribute but the row's key, so what a reference holds (`t` for a stored
// title; `q`, the search's own `also` and `pages`, and `i` for a service's hit;
// the medium) is the server's to decide and this file never lists it. What is
// stored is what a service said, and the add costs no second lookup.

async function addFoundTitle(result) {
    const { key: _key, ...named } = result.dataset;
    const added = await api(
        '/api/rankings/boards/' + encodeURIComponent(boardUid()) + '/items',
        'POST', named);
    if (added.added) bumpVersion();
    return added.key;
}
