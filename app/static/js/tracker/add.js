// Recording something by hand: a show (search, pick, pick a season) and a film
// (a date and a search).
//
// One file because both are the same bargain — the server is told which title by
// its id map and answers with the recomputed month — and both change together
// when that payload changes.

// ---- Add-show modal: search -> pick show -> pick season -> POST add ----
let showSearchTimer = null;
let searchResults = [];
let pickedShow = null;

function openAddShow() {
    document.getElementById('addSearchInput').value = '';
    document.getElementById('addSearchResults').innerHTML = '';
    document.getElementById('addSeasonPick').hidden = true;
    // A closed month has nothing live to bucket against, so adding to one means
    // recording something as finished during it. Say which mode this is before
    // anything is picked.
    document.getElementById('addShowTitle').textContent =
        monthClosed ? '➕ Add a finished show' : '➕ Add show';
    document.getElementById('addShowCompletedNote').hidden = !monthClosed;
    pickedShow = null;
    document.getElementById('addShowModal').classList.add('open');
    document.getElementById('addSearchInput').focus();
}

function closeAddShow() {
    document.getElementById('addShowModal').classList.remove('open');
}

function onAddSearchInput() {
    clearTimeout(showSearchTimer);
    const q = document.getElementById('addSearchInput').value.trim();
    document.getElementById('addSeasonPick').hidden = true;
    if (!q) { document.getElementById('addSearchResults').innerHTML = ''; return; }
    showSearchTimer = setTimeout(() => runAddSearch(q), 300);
}

async function runAddSearch(q) {
    const host = document.getElementById('addSearchResults');
    host.innerHTML = '<div class="distrakt-empty">Searching…</div>';
    const url = `/api/distrakt/search?q=${encodeURIComponent(q)}`;
    console.log('[distrakt] search ->', url);
    try {
        const res = await fetch(url);
        console.log('[distrakt] search response status', res.status);
        const d = await res.json();
        console.log('[distrakt] search response body', d);
        if (!d.ok) {
            console.error('[distrakt] search failed:', d.error);
            host.innerHTML = `<div class="distrakt-empty">${esc(d.error || 'Search failed.')}</div>`;
            toast(d.error || 'Search failed', false);
            return;
        }
        searchResults = d.results || [];
        console.log('[distrakt] search results count', searchResults.length);
        renderSearchResults(searchResults);
    } catch (e) {
        console.error('[distrakt] search request threw', e);
        host.innerHTML = '<div class="distrakt-empty">Search failed.</div>';
    }
}

function renderSearchResults(results) {
    const host = document.getElementById('addSearchResults');
    if (!results.length) { host.innerHTML = '<div class="distrakt-empty">No matches.</div>'; return; }
    host.innerHTML = results.map((r, i) => `
        <div class="distrakt-search-row" onclick="pickShow(${i})">
            <span class="distrakt-title">${esc(r.title)}</span>
            <span class="distrakt-year">${esc(r.year || '')}</span>
            <span class="distrakt-network">${esc(r.network || '')}</span>
        </div>
    `).join('');
}

async function pickShow(i) {
    pickedShow = searchResults[i];
    if (!pickedShow) return;
    const panel = document.getElementById('addSeasonPick');
    const list = document.getElementById('addSeasonList');
    document.getElementById('addSeasonShowTitle').textContent = pickedShow.title;
    panel.hidden = false;
    list.innerHTML = '<div class="distrakt-empty">Loading seasons…</div>';
    const url = `/api/distrakt/seasons?id=${encodeURIComponent(pickedShow.ids.trakt)}`;
    console.log('[distrakt] seasons ->', url);
    try {
        const res = await fetch(url);
        console.log('[distrakt] seasons response status', res.status);
        const d = await res.json();
        console.log('[distrakt] seasons response body', d);
        if (!d.ok) {
            console.error('[distrakt] seasons failed:', d.error);
            list.innerHTML = `<div class="distrakt-empty">${esc(d.error || 'Could not load seasons.')}</div>`;
            toast(d.error || 'Could not load seasons', false);
            return;
        }
        renderSeasons(d.seasons || []);
    } catch (e) {
        console.error('[distrakt] seasons request threw', e);
        list.innerHTML = '<div class="distrakt-empty">Could not load seasons.</div>';
    }
}

function renderSeasons(seasons) {
    const list = document.getElementById('addSeasonList');
    if (!seasons.length) { list.innerHTML = '<div class="distrakt-empty">No aired seasons found.</div>'; return; }
    list.innerHTML = seasons.map(s => `
        <button type="button" class="btn-ghost small" onclick="addPickedShow(${s.season})">
            S${String(s.season).padStart(2, '0')} (${s.episode_count} eps)
        </button>
    `).join('');
}

async function addPickedShow(season) {
    if (!pickedShow) return;
    const asFinished = monthClosed;
    try {
        const res = await fetch(asFinished ? '/api/distrakt/add-completed' : '/api/distrakt/add', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                year: window.DISTRAKT_YEAR, month: window.DISTRAKT_MONTH,
                ids: pickedShow.ids,
                title: pickedShow.title, network: pickedShow.network, season
            })
        });
        const d = await res.json();
        if (!d.ok) throw new Error(d.error || 'failed');
        const label = `${pickedShow.title} S${String(season).padStart(2, '0')}`;
        toast(asFinished ? `Recorded ${label} as finished` : `Added ${label}`, true);
        closeAddShow();
        applyMonthResponse(d);  // mutation returns the recomputed month (1d)
    } catch (e) {
        toast(e.message || 'Could not add show', false);
    }
}

// ---- Add a film ----
// Films have no roster, no buckets and no progress: one is a play on a day. So
// this flow is a date and a search, and the month it lands in follows from the
// date rather than from whichever month happens to be on screen.
let movieSearchTimer = null;
let movieResults = [];

function openAddMovie() {
    const input = document.getElementById('addMovieDate');
    // Defaults to the month being looked at, since that is almost always the
    // one being filled in — the 1st, or today when it is the current month.
    const now = new Date();
    const viewing = (now.getFullYear() === window.DISTRAKT_YEAR && (now.getMonth() + 1) === window.DISTRAKT_MONTH);
    input.value = viewing
        ? now.toISOString().slice(0, 10)
        : `${window.DISTRAKT_YEAR}-${String(window.DISTRAKT_MONTH).padStart(2, '0')}-01`;
    input.max = new Date().toISOString().slice(0, 10);
    document.getElementById('addMovieSearch').value = '';
    document.getElementById('addMovieResults').innerHTML = '';
    movieResults = [];
    document.getElementById('addMovieModal').classList.add('open');
    document.getElementById('addMovieSearch').focus();
}

function closeAddMovie() {
    document.getElementById('addMovieModal').classList.remove('open');
}

function onAddMovieSearchInput() {
    clearTimeout(movieSearchTimer);
    const q = document.getElementById('addMovieSearch').value.trim();
    if (!q) { document.getElementById('addMovieResults').innerHTML = ''; return; }
    movieSearchTimer = setTimeout(() => runMovieSearch(q), 300);
}

async function runMovieSearch(q) {
    const host = document.getElementById('addMovieResults');
    host.innerHTML = '<div class="distrakt-empty">Searching…</div>';
    try {
        const res = await fetch('/api/distrakt/search-movie?q=' + encodeURIComponent(q));
        const d = await res.json();
        if (!d.ok) throw new Error(d.error || 'Search failed.');
        movieResults = d.results || [];
        host.innerHTML = movieResults.length
            ? movieResults.map((r, i) => `
                <div class="distrakt-search-row" onclick="addPickedMovie(${i})">
                    <span class="distrakt-title">${esc(r.title)}</span>
                    <span class="distrakt-year">${esc(r.year || '')}</span>
                    <span class="distrakt-network">${r.runtime ? esc(r.runtime) + ' min' : ''}</span>
                </div>`).join('')
            : '<div class="distrakt-empty">No matches.</div>';
    } catch (e) {
        host.innerHTML = `<div class="distrakt-empty">${esc(e.message || 'Search failed.')}</div>`;
    }
}

async function addPickedMovie(i) {
    const picked = movieResults[i];
    if (!picked) return;
    try {
        const res = await fetch('/api/distrakt/add-movie', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                ids: picked.ids, title: picked.title, year: picked.year,
                watched_on: document.getElementById('addMovieDate').value,
                // Which month to re-render afterwards: the one on screen, which
                // is not necessarily the one the film was filed under.
                year_view: window.DISTRAKT_YEAR, month_view: window.DISTRAKT_MONTH,
            }),
        });
        const d = await res.json();
        if (!d.ok) throw new Error(d.error || 'failed');
        const day = document.getElementById('addMovieDate').value;
        toast(`Recorded ${picked.title} — watched ${day}`, true);
        closeAddMovie();
        applyMonthResponse(d);
    } catch (e) {
        toast(e.message || 'Could not add that film', false);
    }
}
