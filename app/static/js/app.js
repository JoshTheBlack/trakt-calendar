// ---- Page context (from <body> data-* attributes) ----
const BODY = document.body;
const MONTH = BODY.dataset.month;
const YEAR = BODY.dataset.year;
const ENDPOINT = BODY.dataset.endpoint;
const currentTotalShows = parseInt(BODY.dataset.total, 10) || 0;
const currentTotal = currentTotalShows;
const STATE_URL = `/api/state?month=${MONTH}&year=${YEAR}&endpoint=${encodeURIComponent(ENDPOINT)}`;

let notWatching = new Set();
let historyLog = [];
let lastKnownStats = { total: null, watching: null, notWatching: null };
let currentShowIds = [];

// ---- Endpoint switching (requirement D) ----
function switchEndpoint(key) {
    const params = new URLSearchParams(window.location.search);
    params.set('endpoint', key);
    params.set('month', MONTH);
    params.set('year', YEAR);
    window.location.search = params.toString();
}

// ---- Layout controls: card style + day packing ----
// Applied instantly via <body> classes (pure CSS), then persisted to settings.
function updateCols() {
    const b = document.body;
    // Column cap per style: poster-only wall is compact; beside cards are wide.
    const cap = b.classList.contains('card-poster') ? 6 : (b.classList.contains('card-horizontal') ? 2 : 5);
    document.querySelectorAll('.day-block').forEach(block => {
        const n = block.querySelectorAll('.card').length;
        block.style.setProperty('--cols', Math.max(1, Math.min(n, cap)));
    });
}

async function setLayout(key, value) {
    if (key === 'card_style') {
        document.body.classList.remove('card-vertical', 'card-horizontal', 'card-poster');
        document.body.classList.add('card-' + value);
    } else if (key === 'day_packing') {
        document.body.classList.remove('pack-stacked', 'pack-packed');
        document.body.classList.add('pack-' + value);
    }
    updateCols();
    try {
        await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ [key]: value })
        });
    } catch (e) { console.error(e); }
}

document.addEventListener('DOMContentLoaded', updateCols);

// Poster-only wall: if a card is too close to the right edge, open its hover panel
// to the LEFT so it never runs off-screen (and so it can't flicker-wrap).
document.addEventListener('mouseover', (e) => {
    const card = e.target.closest && e.target.closest('.card');
    if (!card) return;
    if (document.body.classList.contains('card-poster')) {
        const panel = parseInt(getComputedStyle(document.body).getPropertyValue('--panel-w')) || 300;
        const r = card.getBoundingClientRect();
        card.classList.toggle('flip-left', r.right + panel + 24 > window.innerWidth);
    } else if (card.classList.contains('flip-left')) {
        card.classList.remove('flip-left');
    }
});

// ---- Sonarr / Radarr / Seerr integration ----
let arrStatus = { sonarr: { configured: false, reachable: false }, radarr: { configured: false, reachable: false }, seer: { configured: false, reachable: false } };
let libraryIds = { sonarr: new Set(), radarr: new Set(), seer: new Set() };

async function refreshArrStatus() {
    try {
        const res = await fetch('/api/integrations/status', { cache: 'no-store' });
        arrStatus = await res.json();
    } catch (e) { /* keep last-known status */ }
    applyArrStatus();
}

async function refreshLibrary() {
    try {
        const res = await fetch('/api/integrations/library', { cache: 'no-store' });
        const d = await res.json();
        libraryIds = {
            sonarr: new Set((d.sonarr || []).map(String)),
            radarr: new Set((d.radarr || []).map(String)),
            seer: new Set((d.seer || []).map(String)),
        };
    } catch (e) { /* keep last-known library */ }
    applyLibraryStatus();
}

// The id each service matches on: Sonarr = TVDB, Radarr/Seerr = TMDB.
function libIdFor(kind, ds) {
    return kind === 'sonarr' ? ds.tvdb : ds.tmdb;
}

function markInLibrary(btn, titleText) {
    btn.classList.add('in-library');
    btn.classList.remove('busy');
    btn.dataset.added = '1';
    btn.dataset.busy = '';
    btn.disabled = false;
    if (titleText) btn.title = titleText;
}

function applyLibraryStatus() {
    document.querySelectorAll('.arr-btn').forEach(btn => {
        if (btn.dataset.busy === '1') return;
        const kind = btn.dataset.arr;
        const card = btn.closest('.card');
        const id = libIdFor(kind, card ? card.dataset : btn.dataset);
        if (id && libraryIds[kind] && libraryIds[kind].has(String(id))) {
            markInLibrary(btn, 'Already in ' + kind.charAt(0).toUpperCase() + kind.slice(1));
        }
    });
}

function applyArrStatus() {
    document.querySelectorAll('.arr-btn').forEach(btn => {
        if (btn.dataset.busy === '1' || btn.dataset.added === '1') return;
        const st = arrStatus[btn.dataset.arr] || {};
        const ok = st.configured && st.reachable;
        btn.disabled = !ok;
        btn.classList.toggle('unreachable', !ok);
        const name = btn.dataset.arr.charAt(0).toUpperCase() + btn.dataset.arr.slice(1);
        btn.title = ok ? ('Add to ' + name) : (name + ' is unreachable');
    });
}

async function addToArr(el, event) {
    if (event) event.stopPropagation();
    if (el.disabled) return;
    const src = el.dataset.media ? el.dataset : (el.closest('.card') ? el.closest('.card').dataset : {});
    const payload = { target: el.dataset.arr, media: src.media, tvdb: src.tvdb || null, tmdb: src.tmdb || null, title: src.title || '' };
    const original = el.innerHTML;
    el.dataset.busy = '1'; el.disabled = true; el.classList.add('busy'); el.innerHTML = '⏳';
    try {
        const res = await fetch('/api/integrations/add', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const d = await res.json();
        if (d.ok) {
            el.innerHTML = original;
            markInLibrary(el, d.message || 'Added');
            const id = libIdFor(el.dataset.arr, el.dataset.media ? el.dataset : (el.closest('.card') || { dataset: {} }).dataset);
            if (id && libraryIds[el.dataset.arr]) libraryIds[el.dataset.arr].add(String(id));
            toast(d.message || 'Added', true);
        } else {
            el.innerHTML = original; el.classList.remove('busy'); el.dataset.busy = ''; el.disabled = false;
            toast(d.error || 'Could not add', false);
        }
    } catch (e) {
        el.innerHTML = original; el.classList.remove('busy'); el.dataset.busy = ''; el.disabled = false;
        toast('Request failed', false);
    }
}

// Add every *watching* item on this page to Sonarr/Radarr. Each add runs via the
// same per-item path (so it toasts individually) with limited concurrency.
async function addAllToArr() {
    // Only the Sonarr/Radarr buttons (not Seerr) — this month's endpoint is TV-only or movie-only.
    const btns = [...document.querySelectorAll('.card:not(.not-watching) .arr-btn')]
        .filter(b => b.dataset.arr !== 'seer' && !b.disabled && b.dataset.added !== '1' && b.dataset.busy !== '1');
    if (!btns.length) {
        toast('Nothing to add — check items are watching and Sonarr/Radarr are reachable.', false);
        return;
    }
    if (!confirm(`Add ${btns.length} watching item${btns.length === 1 ? '' : 's'} to your library?`)) return;
    toast(`Adding ${btns.length} item${btns.length === 1 ? '' : 's'}…`, true);
    let i = 0;
    const worker = async () => { while (i < btns.length) { await addToArr(btns[i++]); } };
    await Promise.all([worker(), worker(), worker()]);  // 3 concurrent
}

function toast(message, ok) {
    let host = document.getElementById('toastHost');
    if (!host) { host = document.createElement('div'); host.id = 'toastHost'; document.body.appendChild(host); }
    const t = document.createElement('div');
    t.className = 'toast ' + (ok ? 'ok' : 'err');
    t.textContent = message;
    host.appendChild(t);
    while (host.children.length > 6) host.firstChild.remove();  // don't flood on bulk add
    requestAnimationFrame(() => t.classList.add('show'));
    setTimeout(() => { t.classList.remove('show'); setTimeout(() => t.remove(), 300); }, 4200);
}

document.addEventListener('DOMContentLoaded', () => {
    refreshArrStatus();
    refreshLibrary();
    setInterval(() => { refreshArrStatus(); refreshLibrary(); }, 60000);
});

// ---- Hide / show not-watching (requirement E) ----
async function toggleHideNotWatching() {
    const hide = !BODY.classList.contains('hide-not-watching');
    BODY.classList.toggle('hide-not-watching', hide);
    const label = document.getElementById('hideToggleLabel');
    const btn = document.getElementById('hideToggle');
    label.textContent = hide ? '🚫 Hiding not-watching' : '👁️ Showing all';
    btn.classList.toggle('active', hide);
    // Persist the preference so it sticks across reloads.
    try {
        await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ hide_not_watching: hide })
        });
    } catch (e) { console.error(e); }
}

// ---- State storage ----
async function loadState() {
    const res = await fetch(STATE_URL, { method: 'GET', cache: 'no-store' });
    if (!res.ok) throw new Error('Failed to load state: ' + res.status);
    return res.json();
}

async function saveState() {
    const payload = {
        notWatching: Array.from(notWatching),
        history: historyLog,
        lastCount: currentTotalShows,
        lastShowIds: currentShowIds
    };
    const res = await fetch(STATE_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    });
    if (!res.ok) throw new Error('Failed to save state: ' + res.status);
    return res.json();
}

function setSyncStatus(ok, message) {
    const el = document.getElementById('syncStatus');
    if (!el) return;
    el.textContent = ok ? '🟢 Storage Connected' : ('🔴 ' + (message || 'Storage Error'));
}

document.addEventListener('DOMContentLoaded', async () => {
    try {
        const state = await loadState();
        notWatching = new Set(state.notWatching || []);
        historyLog = state.history || [];
        setSyncStatus(true);

        const now = new Date();
        const ts = now.getHours() + ':' + String(now.getMinutes()).padStart(2, '0');
        const todayKey = now.toISOString().slice(0, 10);

        if (historyLog.length === 0 || historyLog[historyLog.length - 1].count !== currentTotal) {
            historyLog.push({ time: ts, count: currentTotal, date: todayKey });
            if (historyLog.length > 3) historyLog.shift();
        }

        document.getElementById('historyLog').innerHTML = '<strong>History:</strong>' +
            [...historyLog].reverse().map(i => `<div style="display:flex; justify-content:space-between;"><span>${getRelativeDayLabel(i.date)} ${i.time}</span><span>${i.count} items</span></div>`).join('');

        document.querySelectorAll('.card').forEach(card => {
            setCardState(card, notWatching.has(card.getAttribute('data-id')));
        });

        currentShowIds = Array.from(document.querySelectorAll('.card')).map(c => c.getAttribute('data-id'));

        if (Array.isArray(state.lastShowIds)) {
            const previousShowIds = new Set(state.lastShowIds);
            document.querySelectorAll('.card').forEach(card => {
                const id = card.getAttribute('data-id');
                if (id && !previousShowIds.has(id)) card.classList.add('is-new');
            });
        }

        const previousCount = state.lastCount;
        const deltaMsgElement = document.getElementById('deltaMsg');
        if (previousCount !== null && previousCount !== undefined) {
            if (currentTotalShows > previousCount) {
                deltaMsgElement.textContent = `📈 (+${currentTotalShows - previousCount} since last run)`;
                deltaMsgElement.style.color = '#34d399';
            } else if (currentTotalShows < previousCount) {
                deltaMsgElement.textContent = `📉 (-${previousCount - currentTotalShows} since last run)`;
                deltaMsgElement.style.color = '#f87171';
            } else {
                deltaMsgElement.textContent = `✅ Perfect Match`;
                deltaMsgElement.style.color = '#a1a1aa';
            }
        } else {
            deltaMsgElement.textContent = `(Initial Tracking)`;
        }

        updateStats();
        await saveState();
    } catch (e) {
        console.error(e);
        setSyncStatus(false, 'Load failed');
        updateStats();
    }
});

function getRelativeDayLabel(dateStr) {
    if (!dateStr) return 'Today';
    const today = new Date();
    const todayKey = today.toISOString().slice(0, 10);
    const yesterday = new Date(today);
    yesterday.setDate(yesterday.getDate() - 1);
    const yesterdayKey = yesterday.toISOString().slice(0, 10);
    if (dateStr === todayKey) return 'Today';
    if (dateStr === yesterdayKey) return 'Yesterday';
    return new Date(dateStr + 'T00:00:00').toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function setCardState(card, isNotWatching) {
    card.classList.toggle('not-watching', isNotWatching);
    const btn = card.querySelector('.watch-toggle');
    btn.querySelector('.icon-open').style.display = isNotWatching ? 'none' : 'block';
    btn.querySelector('.icon-closed').style.display = isNotWatching ? 'block' : 'none';
}

async function toggleWatch(btn, event) {
    if (event) event.stopPropagation();
    const card = btn.closest('.card');
    const id = card.getAttribute('data-id');
    const isNotWatching = !card.classList.contains('not-watching');
    setCardState(card, isNotWatching);
    if (isNotWatching) notWatching.add(id); else notWatching.delete(id);
    updateStats();
    try {
        await saveState();
        setSyncStatus(true);
    } catch (e) {
        console.error(e);
        setSyncStatus(false, 'Save failed');
        alert('⚠️ Your change could not be saved to the server.');
    }
}

function popStat(el) {
    el.classList.remove('stat-pop');
    void el.offsetWidth;
    el.classList.add('stat-pop');
}

function updateStats() {
    const total = currentTotalShows;
    const actualNotWatching = document.querySelectorAll('.card.not-watching').length;
    const actualWatching = total - actualNotWatching;
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
}

async function clearData() {
    if (!confirm('Reset watching status and count tracking for this month/endpoint?')) return;
    notWatching.clear();
    historyLog = [];
    document.querySelectorAll('.card').forEach(card => setCardState(card, false));
    document.getElementById('deltaMsg').textContent = `(Reset successfully)`;
    document.getElementById('historyLog').innerHTML = '';
    updateStats();
    try {
        await saveState();
        setSyncStatus(true);
    } catch (e) {
        console.error(e);
        setSyncStatus(false, 'Save failed');
        alert('⚠️ Reset could not be saved to the server.');
    }
}

// ---- Settings modal (requirement C) ----
async function openSettings() {
    try {
        const res = await fetch('/api/settings', { cache: 'no-store' });
        const s = await res.json();
        document.getElementById('s_client_id').value = s.trakt_client_id || '';
        document.getElementById('s_access_token').value = s.trakt_access_token || '';
        document.getElementById('s_timezone').value = s.timezone || '';
        document.getElementById('s_endpoint').value = s.endpoint || 'shows/new';
        document.getElementById('s_genres').value = s.genres || '';
        document.getElementById('s_countries').value = s.countries || '';
        document.getElementById('s_networks').value = (s.network_filter || []).join(', ');
        document.getElementById('s_limit').value = s.pagination_limit || 300;
        document.getElementById('s_cache').value = (s.cache_ttl_minutes ?? 720);
        document.getElementById('s_hide').checked = !!s.hide_not_watching;
        // Sonarr / Radarr
        document.getElementById('s_sonarr_url').value = s.sonarr_url || '';
        document.getElementById('s_sonarr_key').value = s.sonarr_api_key || '';
        ensureOption(document.getElementById('s_sonarr_qp'), s.sonarr_quality_profile_id, 'Profile #' + s.sonarr_quality_profile_id);
        ensureOption(document.getElementById('s_sonarr_rf'), s.sonarr_root_folder, s.sonarr_root_folder);
        document.getElementById('s_radarr_url').value = s.radarr_url || '';
        document.getElementById('s_radarr_key').value = s.radarr_api_key || '';
        ensureOption(document.getElementById('s_radarr_qp'), s.radarr_quality_profile_id, 'Profile #' + s.radarr_quality_profile_id);
        ensureOption(document.getElementById('s_radarr_rf'), s.radarr_root_folder, s.radarr_root_folder);
        document.getElementById('s_seer_url').value = s.seer_url || '';
        document.getElementById('s_seer_key').value = s.seer_api_key || '';
    } catch (e) { console.error(e); }
    document.getElementById('settingsModal').classList.add('open');
}

// Keep a saved <select> value selectable even before options are loaded from Sonarr/Radarr.
function ensureOption(sel, value, label) {
    if (!value) return;
    if (![...sel.options].some(o => o.value === String(value))) {
        const o = document.createElement('option');
        o.value = value; o.textContent = label || value;
        sel.appendChild(o);
    }
    sel.value = String(value);
}

async function loadArrOptions(kind) {
    const url = document.getElementById('s_' + kind + '_url').value.trim();
    const key = document.getElementById('s_' + kind + '_key').value.trim();
    if (!url || !key) { toast('Enter the ' + kind + ' URL and API key first', false); return; }
    try {
        const res = await fetch('/api/integrations/options', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ kind, url, api_key: key })
        });
        const d = await res.json();
        if (!d.ok) { toast(d.error || ('Could not load ' + kind + ' options'), false); return; }
        const qp = document.getElementById('s_' + kind + '_qp');
        const rf = document.getElementById('s_' + kind + '_rf');
        const savedQp = qp.value, savedRf = rf.value;
        qp.innerHTML = (d.profiles || []).map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join('');
        rf.innerHTML = (d.folders || []).map(f => `<option value="${esc(f.path)}">${esc(f.path)}</option>`).join('');
        if (savedQp) qp.value = savedQp;
        if (savedRf) rf.value = savedRf;
        toast(kind.charAt(0).toUpperCase() + kind.slice(1) + ' options loaded', true);
    } catch (e) { toast('Could not load ' + kind + ' options', false); }
}

function closeSettings() { document.getElementById('settingsModal').classList.remove('open'); }

async function saveSettings(event) {
    event.preventDefault();
    const payload = {
        trakt_client_id: document.getElementById('s_client_id').value.trim(),
        trakt_access_token: document.getElementById('s_access_token').value.trim(),
        timezone: document.getElementById('s_timezone').value.trim() || 'Europe/Athens',
        endpoint: document.getElementById('s_endpoint').value,
        genres: document.getElementById('s_genres').value.trim(),
        countries: document.getElementById('s_countries').value.trim(),
        network_filter: document.getElementById('s_networks').value,
        pagination_limit: parseInt(document.getElementById('s_limit').value, 10) || 300,
        cache_ttl_minutes: parseInt(document.getElementById('s_cache').value, 10) || 0,
        hide_not_watching: document.getElementById('s_hide').checked,
        sonarr_url: document.getElementById('s_sonarr_url').value.trim(),
        sonarr_api_key: document.getElementById('s_sonarr_key').value.trim(),
        sonarr_quality_profile_id: parseInt(document.getElementById('s_sonarr_qp').value, 10) || 0,
        sonarr_root_folder: document.getElementById('s_sonarr_rf').value,
        radarr_url: document.getElementById('s_radarr_url').value.trim(),
        radarr_api_key: document.getElementById('s_radarr_key').value.trim(),
        radarr_quality_profile_id: parseInt(document.getElementById('s_radarr_qp').value, 10) || 0,
        radarr_root_folder: document.getElementById('s_radarr_rf').value,
        seer_url: document.getElementById('s_seer_url').value.trim(),
        seer_api_key: document.getElementById('s_seer_key').value.trim()
    };
    try {
        const res = await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        if (!res.ok) throw new Error('save failed');
        window.location.reload();
    } catch (e) {
        console.error(e);
        alert('⚠️ Could not save settings.');
    }
    return false;
}

// ---- Season info tile enrichment (requirement F) ----
// Lazily fetch each show's current-season summary as its card scrolls into view,
// so the initial page render stays fast. Results are cached server-side.
function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function enrichSeasonInfo(card) {
    const el = card.querySelector('[data-role="season-info"]');
    if (!el || el.dataset.loaded) return;
    el.dataset.loaded = '1';
    const id = card.dataset.traktId;
    const media = card.dataset.media;
    const season = card.dataset.season;
    if (!id || media === 'movie' || season === '') return;
    try {
        const res = await fetch(`/api/tile?media=${encodeURIComponent(media)}&id=${encodeURIComponent(id)}&season=${encodeURIComponent(season)}`);
        const d = await res.json();
        if (!d.ok) return;
        const parts = [];
        if (d.episode_count) parts.push(`<span class="si">📋 <b>${d.episode_count}</b> ep${d.episode_count === 1 ? '' : 's'} · S${season}</span>`);
        if (d.last_aired) parts.push(`<span class="si">🏁 Latest: <b>${esc(d.last_aired)}</b></span>`);
        if (d.next_aired) parts.push(`<span class="si next">📡 Next: <b>${esc(d.next_aired)}</b></span>`);
        if (parts.length) {
            el.innerHTML = parts.join('');
            el.hidden = false;
        }
    } catch (e) { /* non-fatal */ }
}

document.addEventListener('DOMContentLoaded', () => {
    const cards = document.querySelectorAll('.card[data-season]:not([data-season=""])');
    if (!('IntersectionObserver' in window)) {
        cards.forEach(enrichSeasonInfo);
        return;
    }
    const io = new IntersectionObserver((entries, obs) => {
        entries.forEach(entry => {
            if (entry.isIntersecting) { enrichSeasonInfo(entry.target); obs.unobserve(entry.target); }
        });
    }, { rootMargin: '200px' });
    cards.forEach(c => io.observe(c));
});

// ---- Details modal (requirement G) ----
async function openDetails(card, event) {
    if (event) {
        const interactive = event.target.closest('.watch-toggle, .trakt-btn, a, button');
        if (interactive) return;
    }
    const title = card.dataset.title || card.querySelector('.show-title')?.textContent || 'Details';
    const poster = card.dataset.poster || '/static/images/nopostertv.png';
    const media = card.dataset.media;
    const id = card.dataset.traktId;
    const season = card.dataset.season;

    document.getElementById('detailsTitle').textContent = title;
    buildDetailsActions(card, media, title);
    document.getElementById('detailsBody').innerHTML = '<div class="details-loading">⏳ Loading details…</div>';
    document.getElementById('detailsModal').classList.add('open');

    if (!id) {
        document.getElementById('detailsBody').innerHTML = '<div class="d-empty">No Trakt id available for this item.</div>';
        return;
    }
    try {
        const q = `media=${encodeURIComponent(media)}&id=${encodeURIComponent(id)}` + (season ? `&season=${encodeURIComponent(season)}` : '');
        const res = await fetch(`/api/details?${q}`);
        const d = await res.json();
        if (!d.ok) throw new Error(d.error || 'failed');
        renderDetails(d, poster, media, season);
    } catch (e) {
        console.error(e);
        document.getElementById('detailsBody').innerHTML = '<div class="d-empty">⚠️ Could not load details from Trakt.</div>';
    }
}

// Add-to-library buttons in the details modal's top bar (arr + Seerr, if configured).
function buildDetailsActions(card, media, title) {
    const actions = document.getElementById('detailsActions');
    actions.innerHTML = '';
    const labels = {
        sonarr: 'Add to Sonarr', radarr: 'Add to Radarr', seer: 'Request on Seerr'
    };
    const targets = [media === 'movie' ? 'radarr' : 'sonarr', 'seer'];
    targets.forEach(kind => {
        const st = arrStatus[kind] || {};
        if (!st.configured) return;
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'arr-btn ' + kind;
        btn.dataset.arr = kind;
        btn.dataset.media = media;
        btn.dataset.tvdb = card.dataset.tvdb || '';
        btn.dataset.tmdb = card.dataset.tmdb || '';
        btn.dataset.title = title;
        btn.innerHTML = `<img src="/static/icons/${kind}.png" alt=""> ${labels[kind]}`;
        btn.disabled = !st.reachable;
        if (!st.reachable) { btn.classList.add('unreachable'); btn.title = kind.charAt(0).toUpperCase() + kind.slice(1) + ' is unreachable'; }
        const id = libIdFor(kind, btn.dataset);
        if (id && libraryIds[kind] && libraryIds[kind].has(String(id))) {
            markInLibrary(btn, 'Already in ' + kind.charAt(0).toUpperCase() + kind.slice(1));
        }
        btn.addEventListener('click', (e) => addToArr(btn, e));
        actions.appendChild(btn);
    });
}

// Extract a YouTube video id from the various URL shapes Trakt returns.
function youTubeId(url) {
    const m = String(url).match(/(?:youtube\.com\/(?:watch\?(?:.*&)?v=|embed\/|v\/)|youtu\.be\/)([\w-]{11})/);
    return m ? m[1] : null;
}

function renderDetails(d, poster, media, season) {
    const chips = [];
    if (d.status) chips.push(`<span class="chip">${esc(d.status)}</span>`);
    if (d.network) chips.push(`<span class="chip network">📡 ${esc(d.network)}</span>`);
    if (d.runtime) chips.push(`<span class="chip">⏱️ ${esc(d.runtime)}m</span>`);
    if (d.rating) chips.push(`<span class="chip country">⭐ ${esc(d.rating)}</span>`);
    (d.genres || []).forEach(g => chips.push(`<span class="chip">${esc(g)}</span>`));

    let html = `
        <div class="details-hero">
            <img src="${esc(poster)}" alt="${esc(d.title)} poster">
            <div class="d-meta">
                <div class="d-chips">${chips.join('')}</div>
                ${d.overview ? `<div class="details-overview">${esc(d.overview)}</div>` : '<div class="d-empty">No overview available.</div>'}
            </div>
        </div>`;

    // Trailer (Trakt exposes it via extended=full). Embed YouTube inline, else link out.
    if (d.trailer) {
        const yt = youTubeId(d.trailer);
        html += `<div class="details-section-title">▶️ Trailer</div>`;
        html += yt
            ? `<div class="trailer-embed"><iframe src="https://www.youtube-nocookie.com/embed/${yt}" title="Trailer" loading="lazy" allow="accelerometer; encrypted-media; gyroscope; picture-in-picture" allowfullscreen></iframe></div>`
            : `<a class="pill-btn" href="${esc(d.trailer)}" target="_blank" rel="noopener">Watch trailer ↗</a>`;
    }

    if (d.cast && d.cast.length) {
        html += `<div class="details-section-title">🎭 Cast</div><div class="cast-grid">` +
            d.cast.map(c => `
                <div class="cast-member">
                    ${c.headshot ? `<img class="headshot" src="${esc(c.headshot)}" alt="${esc(c.name)}" loading="lazy">` : `<div class="headshot placeholder">👤</div>`}
                    <div class="c-name">${esc(c.name)}</div>
                    ${c.character ? `<div class="c-char">${esc(c.character)}</div>` : ''}
                </div>`).join('') + `</div>`;
    }

    if (media !== 'movie' && season) {
        html += `<div class="details-section-title">📺 Season ${esc(season)} Episodes</div>`;
        if (d.episodes && d.episodes.length) {
            html += `<div class="ep-list">` + d.episodes.map(ep => `
                <div class="ep-row">
                    <span class="ep-num">E${String(ep.number).padStart(2, '0')}</span>
                    <span class="ep-title">${esc(ep.title)}</span>
                    ${ep.rating ? `<span class="ep-rating">⭐ ${esc(ep.rating)}</span>` : ''}
                    <span class="ep-date">${esc(ep.air_display || 'TBA')}</span>
                </div>`).join('') + `</div>`;
        } else {
            html += `<div class="d-empty">No episode list available for this season yet.</div>`;
        }
    }

    document.getElementById('detailsBody').innerHTML = html;
}

function closeDetails() { document.getElementById('detailsModal').classList.remove('open'); }

document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { closeSettings(); closeDetails(); }
});
