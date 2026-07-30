// ---------------------------------------------------------------------------
// search
// ---------------------------------------------------------------------------

let searchTimer = null;

function onSearchInput() {
    clearTimeout(searchTimer);
    // Debounced because every call reaches a provider, and the route's own
    // budget is there for a script rather than for somebody typing.
    searchTimer = setTimeout(runSearch, 350);
}

async function runSearch() {
    clearTimeout(searchTimer);
    const box = document.getElementById('searchBox');
    const results = document.getElementById('searchResults');
    const query = box.value.trim();
    if (query.length < 2) { results.hidden = true; results.innerHTML = ''; return; }
    results.hidden = false;
    results.innerHTML = '<span class="ranker-preview-hint">Searching…</span>';
    let data;
    try {
        data = await api('/api/rankings/search', 'POST',
                         { query: query, media: document.getElementById('searchMedia').value });
    } catch (e) {
        results.innerHTML = '';
        results.appendChild(Object.assign(document.createElement('span'),
            { className: 'ranker-preview-hint', textContent: e.message }));
        return;
    }
    results.innerHTML = '';
    if (!data.results.length) {
        results.innerHTML = '<span class="ranker-preview-hint">Nothing found.</span>';
        return;
    }
    data.results.forEach(ref => results.appendChild(buildResult(ref)));
    markHeldResults();
    initSearchDrag();
}

function buildResult(ref) {
    const el = document.createElement('div');
    el.className = 'ranker-result';
    el.dataset.key = ref.key;
    el.dataset.ref = JSON.stringify(ref);
    el.title = 'Drag into a tier, or click to add to the pool';
    el.onclick = () => addSearchResult(el);
    const thumb = ref.tmdb
        ? '<img loading="lazy" alt="" src="' +
          esc('/api/rankings/poster?media=' + ref.media + '&tmdb=' + ref.tmdb) +
          '" onerror="this.remove()">'
        : '';
    el.innerHTML = thumb +
        '<span class="ranker-name"><span class="ranker-title">' + esc(ref.title || 'Untitled') +
        '</span><span class="ranker-year">' +
        esc([ref.year, ref.network, ref.runtime ? ref.runtime + 'm' : ''].filter(Boolean).join(' · ')) +
        '</span></span>';
    return el;
}

// A title the board already holds is shown as held rather than hidden: seeing
// that it is already there is the answer somebody searching for it wanted.
function markHeldResults() {
    const held = new Set((state.board.pool || []).concat(
        ...(state.board.categories || []).map(c => c.items)));
    document.querySelectorAll('#searchResults .ranker-result').forEach(el => {
        el.classList.toggle('is-held', held.has(el.dataset.key));
    });
}

async function addSearchResult(el) {
    if (el.classList.contains('is-held')) return;
    const ref = JSON.parse(el.dataset.ref);
    try {
        const added = await api(
            '/api/rankings/boards/' + encodeURIComponent(boardUid()) + '/items',
            'POST', { refs: [ref] });
        if (added.added) bumpVersion();
    } catch (e) { toast(e.message, false); return; }
    state.board.pool.unshift(ref.key);
    const pool = document.getElementById('rankerPool');
    pool.insertBefore(buildRow(ref), pool.firstChild);
    const empty = document.getElementById('poolEmpty');
    if (empty) empty.remove();
    markHeldResults();
    refreshCounts();
    warmPosters([ref.key]);
}
