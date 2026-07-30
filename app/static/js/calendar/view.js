// The calendar's own view state: which month is on screen, this viewer's marks,
// and the tiles that count them.
//
// Everything here is about ONE month as this account sees it. The layout of the
// day blocks is calendar/layout.js; what a card's buttons do to Sonarr/Radarr is
// calendar/arr-buttons.js.

// ---- Page context ----
// A boosted navigation swaps the <body>'s children in place WITHOUT reloading
// this script, so the page context can't be captured once into consts at load
// time — the values would go stale on the first nav. It lives on #pageData,
// which sits INSIDE the swapped region, and is re-read on every (re)init. The
// per-account view prefs go on the persistent <body> instead, for the reason
// calendar/layout.js gives where BODY is declared.

let MONTH, YEAR, ENDPOINT, currentTotalShows, STATE_URL;

function readPageContext() {
    const d = (document.getElementById('pageData') || document.body).dataset;
    MONTH = d.month;
    YEAR = d.year;
    ENDPOINT = d.endpoint;
    currentTotalShows = parseInt(d.total, 10) || 0;
    STATE_URL = `/api/state?month=${MONTH}&year=${YEAR}&endpoint=${encodeURIComponent(ENDPOINT)}`;
}

// The view's own numbers, read from the JSON the server embeds rather than
// counted off the DOM. Counting cards only ever described the cards the page
// happened to be holding; these describe the whole month, which is what the
// stats tiles claim to be about.
let notWatching = new Set();
// The ids this load decided were new, computed over the WHOLE month by the page
// that shipped the shell. Days that arrive afterwards are marked from this one
// answer: a late request cannot recompute it, because the baseline it would diff
// against is the one the shell already committed.
let newIds = new Set();
let showCounts = {};
let watchingCount = 0;
let notWatchingCount = 0;
let lastKnownStats = { total: null, watching: null, notWatching: null };

// ---- State storage ----
// A DELTA endpoint: a toggle sends only the one item that changed, and the
// change-detection baseline (last_count/last_show_ids/history) is written
// separately, once per load. Neither is a read-modify-write of the whole
// document, so two open tabs can't lose each other's marks.
async function saveNotWatchingDelta(itemId, isNotWatching) {
    const res = await fetch(STATE_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ item_id: itemId, not_watching: isNotWatching })
    });
    if (!res.ok) throw new Error('Failed to save state: ' + res.status);
    return res.json();
}

// Storage persistence is silent on success; only a FAILURE is surfaced (a toast),
// so a broken save doesn't lose your not-watching marks without warning.
function setSyncStatus(ok, message) {
    if (!ok) toast('⚠️ ' + (message || 'Storage error') + ' — changes may not be saved.', false);
}

// The month's counts, this viewer's marks and the ids this load called new are
// all computed server-side and shipped with the page, so there is nothing to
// fetch on load and nothing to re-mark after paint. This reads those numbers
// back so a toggle can keep the tiles honest without asking the DOM how many
// cards it currently holds — a question whose answer stops being the month's
// answer as soon as the page renders anything less than all of it.
function readViewData() {
    let data = {};
    const el = document.getElementById('calendarViewData');
    if (el) {
        try {
            data = JSON.parse(el.textContent) || {};
        } catch (e) {
            console.error(e);
        }
    }
    notWatching = new Set(data.notWatching || []);
    newIds = new Set(data.newIds || []);
    showCounts = data.showCounts || {};
    watchingCount = data.watching || 0;
    notWatchingCount = data.notWatchingCount || 0;
    // The tiles are already painted with exactly these values, so remember them
    // as the starting point: the pop animation is for changes the viewer makes,
    // not for the numbers the page arrived with.
    lastKnownStats = { total: currentTotalShows, watching: watchingCount, notWatching: notWatchingCount };
}

// One class flip: which eye the toggle shows is a CSS consequence of it, so the
// server can render a card already in either state without this having run.
function setCardState(card, isNotWatching) {
    card.classList.toggle('not-watching', isNotWatching);
}

// Every card on the page for one show. On All Episodes that is a dozen rows
// across a dozen days, all of them the same decision — the server stores the
// mark once, per show, so the page has to move them together or it spends the
// rest of the session disagreeing with what was saved until a reload.
function cardsForShow(id) {
    return document.querySelectorAll(`.card[data-id="${CSS.escape(id)}"]`);
}

async function toggleWatch(btn, event) {
    if (event) event.stopPropagation();
    const card = btn.closest('.card');
    const id = card.getAttribute('data-id');
    const isNotWatching = !card.classList.contains('not-watching');
    cardsForShow(id).forEach(c => setCardState(c, isNotWatching));
    if (isNotWatching) notWatching.add(id); else notWatching.delete(id);
    // A show can air on a dozen days of one month, and the mark applies to all of
    // them. Move the tiles by the month's count for this show rather than by the
    // cards on screen, which is the same number today and won't be once days
    // arrive separately.
    const cards = showCounts[id] || 0;
    notWatchingCount += isNotWatching ? cards : -cards;
    watchingCount -= isNotWatching ? cards : -cards;
    updateStats();
    try {
        await saveNotWatchingDelta(id, isNotWatching);
    } catch (e) {
        console.error(e);
        setSyncStatus(false, 'Save failed');
    }
}

function popStat(el) {
    el.classList.remove('stat-pop');
    void el.offsetWidth;
    el.classList.add('stat-pop');
}

function updateStats() {
    const total = currentTotalShows;
    const actualNotWatching = notWatchingCount;
    const actualWatching = watchingCount;
    const totalEl = document.getElementById('statTotal');
    const watchingEl = document.getElementById('statWatching');
    const notWatchingEl = document.getElementById('statNotWatching');
    if (lastKnownStats.total !== null && lastKnownStats.total !== total) popStat(totalEl);
    if (lastKnownStats.watching !== null && lastKnownStats.watching !== actualWatching) popStat(watchingEl);
    if (lastKnownStats.notWatching !== null && lastKnownStats.notWatching !== actualNotWatching) popStat(notWatchingEl);
    totalEl.textContent = total;
    watchingEl.textContent = actualWatching;
    notWatchingEl.textContent = actualNotWatching;
    lastKnownStats = { total, watching: actualWatching, notWatching: actualNotWatching };
    updateEmptyDays();
}
