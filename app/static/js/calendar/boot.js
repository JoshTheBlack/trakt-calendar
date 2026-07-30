// The calendar page's wiring: one idempotent init, what a late-arriving day
// block needs, and the document-level keys.
//
// LOADED LAST of the page's scripts, deliberately. These files are ordinary
// scripts sharing one global scope, executed in the order the <head> lists them
// (defer preserves it), and the init below runs the moment this file executes —
// so everything it calls has to be declared already.

// Escape closes whichever overlay is open. Driven off the class rather than a
// list of close functions: the Settings modal is only rendered for admins, so
// calling closeSettings() unconditionally threw for everybody else — which took
// the rest of the handler down with it and left Escape doing nothing at all.
document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    stopDeviceAuthPolling();
    document.querySelectorAll('.modal-overlay.open').forEach(m => m.classList.remove('open'));
});

// ---- One idempotent page init, run on first load AND after every boosted swap ----
// hx-boost swaps the <body>'s children in place without re-running this script, so
// everything that used to hang off its own DOMContentLoaded is gathered here and
// re-run on htmx:afterSwap as well. Document-level delegated listeners (the
// hover-flip in calendar/layout.js, the Konami code in calendar/easter-egg.js, and
// Escape above) are bound once as their file loads and survive swaps, so they
// deliberately stay OUT of this function.
function initCalendarPage() {
    readPageContext();
    initCertPickers();
    updateCols();
    initArrIntegrations();
    initSeasonInfo();
    initDistraktNav();
    initBuildTap();
    readViewData();
    updateEmptyDays();
    initDayChips();
}

// ---- Day blocks that arrive after the page has painted ----
// The shell renders the first few days and each later one fetches itself when it
// is scrolled to, so cards can appear without a navigation. Those cards need three
// things the shell's own cards got for free, and NOT a re-init: re-reading the
// page's embedded view data here would throw away a mark the viewer made while the
// day was still in flight.
function applyViewStateTo(root) {
    if (!root || root.nodeType !== Node.ELEMENT_NODE) return;
    root.querySelectorAll('.card').forEach(card => {
        const id = card.getAttribute('data-id');
        // is-new is the shell's whole-month answer (see newIds).
        if (newIds.has(id)) card.classList.add('is-new');
        // The server rendered not-watching from what was stored when it built this
        // block. A toggle made before the block arrived exists only in memory here,
        // so reconcile against it rather than trusting the markup — otherwise a
        // show hidden a moment ago comes back visible on the days that loaded late.
        setCardState(card, notWatching.has(id));
    });
    // Column packing is per day block and counts that block's (visible) cards, so
    // it has to run for blocks that did not exist when the page initialised.
    updateEmptyDays();
}

// A BOOSTED arrival is not handled here. static/js/boost.js owns those: it has to
// load whichever of this page's scripts the session has not run yet, and only
// then is there an init to call — so it calls the one registered below, on the
// cold load and on every arrival alike.
// What is left here is the CONTENT swap, which only brings one day's cards and
// must NOT re-run the init (see applyViewStateTo). The event is dispatched on what
// the swap PRODUCED, which for a day block replacing its own placeholder (an
// outerHTML swap) is the new section — detail.target is still the placeholder htmx
// just detached, so it is the wrong thing to look inside.
document.addEventListener('htmx:afterSwap', (evt) => {
    if (evt.detail && evt.detail.boosted) return;
    applyViewStateTo(evt.target);
});

registerPage('calendar', initCalendarPage);
