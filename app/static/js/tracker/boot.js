// The tracker page's wiring: the tab strip, the easter-egg audio, and the four
// loads that fill the page in.
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

// ---- Konami code on the distrakt page -> play the easter-egg audio ----
const KONAMI = ['ArrowUp', 'ArrowUp', 'ArrowDown', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'ArrowLeft', 'ArrowRight', 'b', 'a'];
let konamiBuf = [];
document.addEventListener('keydown', (e) => {
    konamiBuf.push(e.key);
    konamiBuf = konamiBuf.slice(-KONAMI.length);
    if (konamiBuf.length === KONAMI.length && konamiBuf.every((k, i) => k === KONAMI[i])) {
        konamiBuf = [];
        new Audio('/static/audio/distrakt.mp3').play().catch(() => {});
    }
});

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
