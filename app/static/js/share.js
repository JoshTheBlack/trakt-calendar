/* The public share page's details panel.
 *
 * THE SAME PANEL THE CALENDAR OPENS, and now literally the same template: the
 * contents are a fragment (templates/_details.html) served by a token-scoped,
 * rate-limited endpoint beside this page's own address, rather than the
 * session-only one the owner's calendar asks. This file used to carry a copy of
 * the calendar's renderer and a copy of its id-query builder, the second renamed
 * only to dodge a clash in the shared global scope — the rename was the tell.
 *
 * THAT ENDPOINT DESCRIBES ONLY WHAT THIS PAGE ALREADY SHOWS, and the address the
 * card carries is shaped by that rule: the server rendered it with the page's own
 * view on it — the month, the calendar, the timezone, everything that decides
 * which airings are on screen — alongside the card's ids, because the server
 * re-reads that exact month and refuses any title it does not contain. A public
 * URL must never be able to turn an id a stranger typed into a call on the
 * owner's credential; carrying the view is what lets the server tell the two
 * apart. Progressive enhancement: with JavaScript off, the card's own outbound
 * button still reaches the full details.
 *
 * WHICH SERVICE ANSWERS IS THE SERVER'S DECISION, on both pages, and the card
 * simply names every id it carries — see `details_url` in app/templating.py for
 * why. A card only one service listed opens on that service's answer here
 * exactly as it does on the owner's own calendar.
 */

function openShareDetails(card, event) {
    if (event) {
        // Let the poster's own outbound link (and any future control) act normally.
        if (event.target.closest('a, button')) return;
    }
    openDetailsPanel(card.dataset.detailsUrl, card.dataset.title || 'Details');
}
