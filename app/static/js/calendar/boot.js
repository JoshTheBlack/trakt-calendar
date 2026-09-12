// The calendar page's wiring: one idempotent init, and the one thing a day block
// that arrives late needs from the page.
//
// LOADED LAST of the page's scripts, deliberately. These files are ordinary
// scripts sharing one global scope, executed in the order the <head> lists them
// (defer preserves it), and the init below runs the moment this file executes —
// so everything it calls has to be declared already.

// NOTHING HERE LISTENS FOR ESCAPE. static/js/ui.js closes every open overlay on
// Escape through closeModal, and the Settings panel's handshake polling stops
// through the close hook calendar/settings.js registers — so a listener of this
// file's own stopping it too would be a second statement of one rule, and the
// polling cannot outlive the panel it runs in.

// ---- One idempotent page init, run on first load AND after every boosted swap ----
// hx-boost swaps the <body>'s children in place without re-running this script, so
// everything that used to hang off its own DOMContentLoaded is gathered here and
// re-run on every boosted arrival as well. Document-level delegated listeners (the
// hover-flip in calendar/layout.js and the Konami code in calendar/easter-egg.js)
// are bound once as their file loads and survive swaps, so they deliberately stay
// OUT of this function.
function initCalendarPage() {
    readPageContext();
    initCertPickers();
    initDistraktNav();
    initBuildTap();
    readViewData();
    syncChipReach();
    initDayChips();
    // LAST, AND FROM HERE RATHER THAN FROM DOMContentLoaded. Following a search
    // result is a BOOSTED navigation: the body is swapped and that event never
    // fires, so a jump aimed at a card highlighted nothing on the one path the
    // feature exists for. Every arrival runs this init — cold load, boosted
    // navigation and Back alike — which is the whole reason it exists.
    //
    // IT IS THE BOOSTED ARRIVAL THAT STILL NEEDS IT. A cold load lands on the
    // anchor by itself now and holds there; a boosted one is scrolled by htmx at
    // swap time and drifts afterwards, by 842px in Firefox. The measurement is in
    // calendar/search.js, beside the loop it kept.
    scrollToJumpTarget();
}

// ---- Day blocks that arrive after the page has painted ----
// A LATE DAY ARRIVES FINISHED, BUT FOR ONE THING. The server draws everything it
// knows onto the block: each card's not-watching state from the stored marks,
// is-new from the shell's own answer (which rides on the placeholder's request),
// the day's column count and whether it collapses, and the state of every
// Sonarr/Radarr/Seerr button. What it cannot know is a mark this page made whose
// save had not landed when the day's request read the marks — calendar/view.js's
// pendingMarks. So the block's cards are reconciled against those alone, and
// where that moved a card the day's packing is worked out again for what is now
// visible. NOT a re-init: re-running the page's init here would re-read its
// embedded data and throw away the tiles' in-session counts.
function applyViewStateTo(root) {
    if (!root || root.nodeType !== Node.ELEMENT_NODE || !pendingMarks.size) return;
    let moved = false;
    root.querySelectorAll('.card').forEach(card => {
        const id = card.getAttribute('data-id');
        if (!pendingMarks.has(id)) return;
        setCardState(card, pendingMarks.get(id));
        moved = true;
    });
    if (moved) syncChipReach();
}

// A BOOSTED arrival is not handled here. static/js/boost.js owns those: it has to
// load whichever of this page's scripts the session has not run yet, and only
// then is there an init to call — so it calls the one registered below, on the
// cold load and on every arrival alike.
// What is left here is the CONTENT swap, which only brings one day's cards and
// must NOT re-run the init (see applyViewStateTo). A day block replaces its own
// placeholder, so what has to be walked is what the swap PRODUCED rather than
// what it was aimed at; static/js/boost.js's swappedContent answers that for
// every swap style at once, and answers null for a whole-page arrival.
document.addEventListener('htmx:after:swap', (evt) => {
    const swapped = swappedContent(evt);
    if (swapped) applyViewStateTo(swapped);
});

registerPage('calendar', initCalendarPage);
