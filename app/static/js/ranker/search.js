// ---------------------------------------------------------------------------
// adding a title a search found
// ---------------------------------------------------------------------------
// THE SEARCH IS MARKUP, AND SO IS ADDING BY CLICKING. The box is the app's one
// title search (_title_search.html) and the server draws the results; a result
// posts itself and is answered with the page, carrying that same search redrawn
// from what its own request stored (_ranker_search_results.html). Whether a
// result is already held is drawn there too, and a show's seasons are a
// checklist form beside the row.
//
// WHAT IS LEFT IS THE ONE ADD A PAGE CANNOT ANSWER: a result DRAGGED straight
// into a tier. The row has to land where the pointer dropped it — a position
// among siblings no swap can name — so this path takes the row's key back and
// puts the row there itself (ranker/dnd.js). It names the result exactly as a
// click does, by the reference the server drew on the result (`t` for a stored
// title, `q` and `i` for a service's hit, and the search's medium), so what is
// stored is what a service said and the add costs no second lookup.

async function addFoundTitle(result) {
    const named = result.dataset;
    const added = await api(
        '/api/rankings/boards/' + encodeURIComponent(boardUid()) + '/items',
        'POST', { t: named.t, q: named.q, i: named.i, media: named.media });
    if (added.added) bumpVersion();
    return added.key;
}
