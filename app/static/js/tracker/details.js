// ---- Show details modal (the shared panel, plus this user's watched episodes) ----
//
// WHAT IS LEFT HERE AFTER THE PANEL BECAME A TEMPLATE. Its contents — including
// the per-service episode ticks, which no other surface draws — are a fragment
// the server renders (templates/_details.html, given a SeasonProgress by
// app/distrakt/episode_progress.py). The tick links, the disagreement note and
// the service names all moved with it; this file used to carry its own table of
// service names beside the macro that already owned them.
//
// THE ADDRESS IS BUILT HERE AND NOT IN MARKUP, unlike the calendar's and the
// share page's, because a roster row is still assembled in JavaScript
// (tracker/rows.js). It is a URL rather than markup, and it names a row the
// viewer can already see: the key and the season come off that row's own dataset,
// and everything the route looks up follows from the row it finds under them.

function openDistraktDetails(row, event) {
    // The row carries its own controls; clicking Abandon must not also open this.
    if (event && event.target.closest('button, a')) return;
    const key = row.dataset.key;
    const season = row.dataset.season;
    const title = row.dataset.title || 'Details';
    const url = `/distrakt/fragments/details?key=${encodeURIComponent(key)}`
        + `&season=${encodeURIComponent(season)}`;
    openDetailsPanel(url, `${title} · S${String(season).padStart(2, '0')}`);
}
