// The tracker page's wiring: the tab strip and the four loads that fill the page
// in.
//
// LOADED LAST of the page's scripts, deliberately. These files are ordinary
// scripts sharing one global scope, executed in the order the <head> lists them
// (defer preserves it), so everything named below is already declared by the
// time this file runs.

// ---- Tabs ----
function switchTab(name) {
    document.querySelectorAll('.distrakt-tab').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
    document.querySelectorAll('.distrakt-panel').forEach(p => { p.hidden = p.dataset.panel !== name; });
}

// The Konami code is calendar/easter-egg.js's, which this page loads too: one
// listener for both pages, reading what page it is on rather than a flag the
// calendar left on `window`. Two copies meant that once the calendar had been
// visited, pressing it here navigated away from the page it opens.

// The emoji map first and awaited: every row's network badge falls back to it, so
// rendering the month before it has arrived draws the fallback emoji for shows
// that have a mapping.
async function initTrackerPage() {
    await loadEmojiMap();
    loadPostLink();
    loadMonthData();
    loadBackfillRange();
}

// Registered rather than hung off DOMContentLoaded, which a boosted arrival does
// not fire — that is static/js/boost.js's to call, once this page's scripts are
// loaded, however the page was reached.
registerPage('tracker', initTrackerPage);
