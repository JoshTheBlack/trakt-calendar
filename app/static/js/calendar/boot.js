// The calendar page's wiring: one idempotent init, and one swap worth acting on.
//
// WHY THIS IS SCRIPT: what the init starts is a MEASUREMENT of the laid-out
// header (calendar/layout.js), which no stylesheet rule, template or route can
// take, and it has to be re-taken whenever a new month lands — a moment only the
// browser sees.
//
// NOTHING RUNS AS THIS FILE LOADS. It declares the init and listens for the
// moment to run it, so it depends on no other file having run first.

// NOTHING HERE LISTENS FOR ESCAPE. Every overlay is a modal <dialog> carrying
// `closedby="any"`, so Escape is the browser's; a panel the server drew for one
// opening removes itself when it closes (`data-modal-discard`, static/js/ui.js)
// — so a listener of this file's own would be a second statement of one rule,
// and nothing a panel was doing can outlive the panel.

// ---- One idempotent page init, run on first load AND after every boosted swap ----
// hx-boost swaps the <body>'s children in place without re-running this script, so
// everything that used to hang off its own DOMContentLoaded is gathered here and
// re-run on every boosted arrival as well. Document-level delegated listeners (the
// hover-flip in calendar/layout.js and the Konami code in calendar/easter-egg.js)
// are bound once as their file loads and survive swaps, so they deliberately stay
// OUT of this function — the run of taps on the version tag joined them, which is
// what took its re-binding and its guard away.
function initCalendarPage() {
    initDistraktNav();
    initDayChips();
}

// A LATE DAY ARRIVES FINISHED, AND NOTHING HERE TOUCHES IT. The server draws all
// of it: each card's watching state from the stored marks, whether it is new
// from the shell's own answer (which rides on the placeholder's request), the
// day's column count and whether it collapses, and the state of every
// Sonarr/Radarr/Seerr button. There used to be one exception — a mark this page
// had made whose save had not landed when the day's request read the marks —
// and it is gone because the page no longer makes a mark ahead of the server:
// pressing the eye posts, and the answer redraws every card for that title. So a
// day landing while a mark is in flight is drawn from whichever of the two
// reached the database first, and the other one's answer redraws it.

// AN ARRIVAL IS static/js/boost.js's to recognise, and it says so with
// `page-arrived` — on the cold load and on every boosted arrival alike, which is
// the same thing as far as this page is concerned. Every page hears it, so the
// init runs only for the page that names itself the calendar.
document.addEventListener('page-arrived', (event) => {
    if (event.detail.page === 'calendar') initCalendarPage();
});

// WHAT IS LEFT HERE IS ONE SWAP: A FILTERS SAVE, which replaces the whole of
// #calendarBody. Re-running the init then is exactly right — the month on screen
// has been replaced, so the address it re-reads and the chips it re-checks are
// the new month's. A DAY BLOCK arriving is not this: it replaces its own
// placeholder, brings one day's cards and needs nothing run over it.
//
// What has to be looked at is what the swap PRODUCED rather than what it was
// aimed at; static/js/boost.js's swappedContent answers that for every swap style
// at once, and answers null for a whole-page arrival.
document.addEventListener('htmx:after:swap', (evt) => {
    const swapped = swappedContent(evt);
    if (!swapped) return;
    if (swapped.id === 'calendarBody' || swapped.querySelector?.('#calendarBody')) {
        initCalendarPage();
    }
});
