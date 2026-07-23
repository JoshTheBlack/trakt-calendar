// ---- Page context (from <body> data-* attributes) ----
const BODY = document.body;
const MONTH = BODY.dataset.month;
const YEAR = BODY.dataset.year;
const ENDPOINT = BODY.dataset.endpoint;
const currentTotalShows = parseInt(BODY.dataset.total, 10) || 0;
const currentTotal = currentTotalShows;
const STATE_URL = `/api/state?month=${MONTH}&year=${YEAR}&endpoint=${encodeURIComponent(ENDPOINT)}`;

// The cache size cap is stored in bytes and edited in megabytes.
const MB = 1024 * 1024;

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
    // In hide mode, size each day's grid to its VISIBLE (watching) cards so packed
    // layout doesn't reserve columns for hidden not-watching items.
    const hiding = b.classList.contains('hide-not-watching');
    const sel = hiding ? '.card:not(.not-watching)' : '.card';
    document.querySelectorAll('.day-block').forEach(block => {
        const n = block.querySelectorAll(sel).length;
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
    // The layout already changed on screen; this only persists it — to this
    // account's own view preferences, so it sticks on the next visit for
    // whoever is signed in, not just an administrator.
    try {
        await fetch('/api/me/prefs', {
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

// The add/request buttons and the health state behind them only exist for an
// administrator, so nobody else polls for them.
document.addEventListener('DOMContentLoaded', () => {
    if (!window.IS_ADMIN) return;
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
    label.textContent = hide ? '🚫 Hiding' : '👁️ Showing all';
    btn.classList.toggle('active', hide);
    updateEmptyDays();
    // Persists to this account's own view preferences (same as setLayout).
    try {
        await fetch('/api/me/prefs', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ hide_not_watching: hide })
        });
    } catch (e) { console.error(e); }
}

// ---- Timezone picker (day/time grouping) ----
// No automatic browser detection: the saved default is a deliberate choice, and
// "use my device timezone" is one click away rather than something silently
// applied on load. Changing it reloads the page, since day headers and air
// times are computed server-side for the viewer's saved zone.
async function setViewerTimezone(tz) {
    if (!tz) return;
    try {
        const res = await fetch('/api/me/timezone', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ timezone: tz })
        });
        if (!res.ok) throw new Error('save failed');
        window.location.reload();
    } catch (e) {
        console.error(e);
        toast('Could not save timezone', false);
    }
}

function useDeviceTimezone() {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (!tz) return;
    const select = document.getElementById('tzSelect');
    if (select) select.value = tz;
    setViewerTimezone(tz);
}

// ---- State storage ----
// A DELTA endpoint: a toggle sends only the one item that changed, and the
// change-detection baseline (last_count/last_show_ids/history) is written
// separately, once per load. Neither is a read-modify-write of the whole
// document, so two open tabs can't lose each other's marks.
async function loadState() {
    const res = await fetch(STATE_URL, { method: 'GET', cache: 'no-store' });
    if (!res.ok) throw new Error('Failed to load state: ' + res.status);
    return res.json();
}

async function saveNotWatchingDelta(itemId, isNotWatching) {
    const res = await fetch(STATE_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ item_id: itemId, not_watching: isNotWatching })
    });
    if (!res.ok) throw new Error('Failed to save state: ' + res.status);
    return res.json();
}

async function saveViewBaseline() {
    const res = await fetch(STATE_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            last_count: currentTotalShows,
            last_show_ids: currentShowIds,
            history: historyLog
        })
    });
    if (!res.ok) throw new Error('Failed to save state: ' + res.status);
    return res.json();
}

// Storage persistence is silent on success; only a FAILURE is surfaced (a toast),
// so a broken save/load doesn't lose your not-watching marks without warning.
function setSyncStatus(ok, message) {
    if (!ok) toast('⚠️ ' + (message || 'Storage error') + ' — changes may not be saved.', false);
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
        await saveViewBaseline();
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
    updateEmptyDays();
}

// In "Hiding" mode, collapse any day whose items are all not-watching (so nothing
// would render under its header). In "Showing all" mode every day is shown.
function updateEmptyDays() {
    const hiding = BODY.classList.contains('hide-not-watching');
    document.querySelectorAll('.day-block').forEach(block => {
        const hide = hiding && !block.querySelector('.card:not(.not-watching)');
        block.classList.toggle('is-empty-hidden', hide);
    });
    updateCols();  // re-pack columns for the now-visible card counts
}


// ---- Settings modal (requirement C) ----
// Credentials are write-only: the server sends back a flag per secret saying
// whether one is stored, never the value. So each credential input renders
// EMPTY, with a placeholder saying whether something is saved, and an empty
// input on save means "leave it as it is". Clearing one is a deliberate act —
// the ✕ next to the field — because otherwise every save would wipe them all.
function applySecretState(secretsSet) {
    document.querySelectorAll('input[data-secret]').forEach(input => {
        const stored = !!(secretsSet || {})[input.name];
        input.value = '';
        input.dataset.stored = stored ? '1' : '';
        input.dataset.clear = '';
        input.placeholder = stored ? 'Saved — leave blank to keep it' : 'Not set';
        const button = input.parentElement && input.parentElement.querySelector('[data-role="clear-secret"]');
        if (button) button.hidden = !stored;
        setSecretHint(input);
    });
}

function setSecretHint(input) {
    const row = input.parentElement;
    const hint = row && row.querySelector('[data-role="secret-hint"]');
    if (!hint) return;
    hint.textContent = input.dataset.clear ? 'Will be cleared when you save.' : '';
}

function clearSecret(button) {
    const input = button.parentElement.querySelector('input[data-secret]');
    if (!input) return;
    input.value = '';
    input.dataset.clear = '1';
    input.placeholder = 'Will be cleared';
    setSecretHint(input);
}

// Each credential input gets a ✕ beside it, built here rather than repeated six
// times in the template.
function buildSecretControls() {
    document.querySelectorAll('input[data-secret]').forEach(input => {
        const row = input.parentElement;
        if (!row || row.querySelector('[data-role="clear-secret"]')) return;
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn-ghost small';
        button.dataset.role = 'clear-secret';
        button.textContent = '✕ Clear';
        button.title = 'Remove the saved value when you save';
        button.hidden = true;
        button.addEventListener('click', () => clearSecret(button));
        const hint = document.createElement('span');
        hint.className = 'hint';
        hint.dataset.role = 'secret-hint';
        input.insertAdjacentElement('afterend', hint);
        input.insertAdjacentElement('afterend', button);
    });
}

// A secret goes into the payload only when the admin typed a new one or asked
// for it to be cleared. Anything else is omitted, which is what tells the server
// to leave the stored value alone.
function collectSecrets() {
    const payload = {};
    document.querySelectorAll('input[data-secret]').forEach(input => {
        const typed = input.value.trim();
        if (typed) payload[input.name] = typed;
        else if (input.dataset.clear) payload[input.name] = null;
    });
    return payload;
}

// The tabs are presentation only — every panel stays in the one <form>, and
// hidden inputs are read by id at save time, so switching tabs never drops a
// pending edit and one Save still writes all four groups.
function showSettingsTab(name) {
    document.querySelectorAll('#settingsModal [data-tab]').forEach(btn => {
        const on = btn.dataset.tab === name;
        btn.classList.toggle('active', on);
        btn.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    document.querySelectorAll('#settingsModal [data-tab-panel]').forEach(panel => {
        panel.hidden = panel.dataset.tabPanel !== name;
    });
    // The OVERLAY is the scroll container (the modal sits at flex-start inside
    // it), so a tall panel scrolled halfway down would leave the next tab
    // opening mid-content.
    const overlay = document.getElementById('settingsModal');
    if (overlay) overlay.scrollTop = 0;
}

async function openSettings() {
    showSettingsTab('server');
    buildSecretControls();
    try {
        const res = await fetch('/api/settings', { cache: 'no-store' });
        const s = await res.json();
        document.getElementById('s_base_url').value = s.public_base_url || '';
        document.getElementById('s_trusted_proxies').value = s.trusted_proxy_ips || '';
        updateProxyHint(s);
        document.getElementById('s_cookie_secure').value = (s.cookie_secure || 'always').toLowerCase();
        updateCookieHint(s);
        document.getElementById('s_client_id').value = s.trakt_client_id || '';
        applySecretState(s.secrets_set);
        updateTokenStatus(s.trakt_token_expires_at);
        updateTraktLoginHints(s);
        document.getElementById('s_timezone').value = s.timezone || '';
        document.getElementById('s_endpoint').value = s.endpoint || 'shows/new';
        document.getElementById('s_limit').value = s.pagination_limit || 300;
        document.getElementById('s_cache').value = (s.cache_ttl_minutes ?? 720);
        document.getElementById('s_calcache').value = (s.calendar_cache_ttl_minutes ?? 10);
        // Stored in bytes; shown in MB, because nobody wants to count zeros.
        document.getElementById('s_cachecap').value = Math.round((s.api_cache_max_bytes ?? 1073741824) / MB);
        document.getElementById('s_hide').checked = !!s.hide_not_watching;
        document.getElementById('s_genres').value = s.genres || '';
        document.getElementById('s_countries').value = s.countries || '';
        document.getElementById('s_networks').value = (s.network_filter || []).join(', ');
        // Sonarr / Radarr
        document.getElementById('s_sonarr_url').value = s.sonarr_url || '';
        ensureOption(document.getElementById('s_sonarr_qp'), s.sonarr_quality_profile_id, 'Profile #' + s.sonarr_quality_profile_id);
        ensureOption(document.getElementById('s_sonarr_rf'), s.sonarr_root_folder, s.sonarr_root_folder);
        document.getElementById('s_radarr_url').value = s.radarr_url || '';
        ensureOption(document.getElementById('s_radarr_qp'), s.radarr_quality_profile_id, 'Profile #' + s.radarr_quality_profile_id);
        ensureOption(document.getElementById('s_radarr_rf'), s.radarr_root_folder, s.radarr_root_folder);
        document.getElementById('s_seer_url').value = s.seer_url || '';
    } catch (e) { console.error(e); }
    loadEncryptionState();
    document.getElementById('settingsModal').classList.add('open');
}

// ---- At-rest encryption panel (admin, Server tab) ----
// The key never touches this app beyond being revealed once for the operator to
// copy into their own environment; every transition is a call to
// /api/admin/encryption, and the panel just reflects the state it reports back.
function encAddButton(row, label, handler, ghost) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn-ghost small' + (ghost ? '' : ' btn-primary-inline');
    btn.textContent = label;
    btn.addEventListener('click', handler);
    row.appendChild(btn);
}

function renderEncryption(s) {
    const status = document.getElementById('s_enc_status');
    const actions = document.getElementById('s_enc_actions');
    const reveal = document.getElementById('s_enc_reveal');
    const err = document.getElementById('s_enc_error');
    actions.innerHTML = '';
    reveal.hidden = true; reveal.innerHTML = '';
    err.hidden = true; err.textContent = '';
    const env = s.env_var || 'ENCRYPTION_KEY';

    if (s.health === 'key_missing') {
        status.innerHTML = '⚠️ Secrets are encrypted, but <code>' + esc(env) + '</code> is not set — ' +
            'they are unreadable but intact. Restore the key and restart. Do <strong>not</strong> ' +
            're-save credentials or re-link while it is missing: that overwrites the encrypted values.';
        return;
    }
    if (s.phase === 'encrypted') {
        status.textContent = '🔒 Stored secrets are encrypted at rest.';
        return;
    }
    if (s.key_valid) {
        status.innerHTML = 'A valid key is set in <code>' + esc(env) + '</code>. Encrypt the stored ' +
            'secrets now — no restart needed.';
        encAddButton(actions, 'Encrypt secrets now', encEncryptNow);
        if (s.phase !== 'pending_encrypt') encAddButton(actions, 'Not now', encOptOut, true);
        return;
    }
    if (s.phase === 'pending_key' || s.phase === 'pending_encrypt') {
        status.innerHTML = 'Waiting for the key. Set <code>' + esc(env) + '</code> in your environment ' +
            '(env, Docker, or compose) and restart the app, then check again.';
        encAddButton(actions, '↻ Check for the key', encVerify);
        encAddButton(actions, 'Not now', encOptOut, true);
        return;
    }
    // phase 'none' or 'opted_out' with no key yet — the first offer.
    status.innerHTML = 'Encrypt the stored credentials and linked tokens so a leaked database ' +
        'file (a backup or snapshot) does not hand them over. The key lives in your environment, ' +
        'never in the database. <strong>You must save the key and never lose it</strong> — losing it ' +
        'means re-linking Trakt and re-entering every API key.';
    encAddButton(actions, 'Generate a key for me', () => encEnable(true));
    encAddButton(actions, 'I’ll set my own key', () => encEnable(false));
    if (s.phase === 'none') encAddButton(actions, 'Not now', encOptOut, true);
}

async function loadEncryptionState() {
    const panel = document.getElementById('s_enc_panel');
    if (!panel) return;  // non-admin page has no encryption panel
    try {
        const res = await fetch('/api/admin/encryption', { cache: 'no-store' });
        const s = await res.json();
        if (!s.ok) { document.getElementById('s_enc_status').textContent = 'Unavailable.'; return; }
        renderEncryption(s);
    } catch (e) { console.error(e); }
}

function encError(message) {
    const err = document.getElementById('s_enc_error');
    err.textContent = message || 'Something went wrong.';
    err.hidden = false;
}

async function encPost(path, body) {
    const res = await fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {}),
    });
    const data = await res.json().catch(() => ({}));
    return { ok: res.ok && data.ok, data };
}

async function encEnable(generate) {
    const { ok, data } = await encPost('/api/admin/encryption/enable', { generate });
    if (!ok) { encError(data.error); return; }
    if (data.restart_required) { encShowReveal(data.key, generate); return; }
    loadEncryptionState();  // a valid key was already present — straight to "encrypt now"
}

function encShowReveal(key, generated) {
    const env = 'ENCRYPTION_KEY';
    const reveal = document.getElementById('s_enc_reveal');
    document.getElementById('s_enc_actions').innerHTML = '';
    let inner = '';
    if (generated) {
        inner += '<p class="hint"><strong>Save this key now — it is shown once and never again.</strong> ' +
            'If you lose it, the encrypted secrets are unrecoverable.</p>' +
            '<div class="share-link-box"><input type="text" id="s_enc_key" readonly value="' + esc(key) + '">' +
            '<button type="button" class="btn-ghost small" onclick="encCopyKey()">Copy</button></div>';
    } else {
        inner += '<p class="hint">Generate a key with:</p>' +
            '<div class="share-link-box"><input type="text" readonly value="python -c &quot;from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())&quot;"></div>';
    }
    inner += '<p class="hint">Set it as <code>' + env + '</code> in your environment (env file, ' +
        'Docker <code>-e</code>, or compose <code>environment:</code>) and restart the app. Then:</p>';
    reveal.innerHTML = inner;
    encAddButton(document.getElementById('s_enc_actions'), '↻ I’ve set the key and restarted — check', encVerify);
    encAddButton(document.getElementById('s_enc_actions'), 'Not now', encOptOut, true);
    reveal.hidden = false;
}

function encCopyKey() {
    const input = document.getElementById('s_enc_key');
    if (!input) return;
    input.select();
    try { navigator.clipboard.writeText(input.value); } catch (_) { document.execCommand('copy'); }
    toast('Key copied — store it somewhere safe', true);
}

async function encVerify() {
    const { ok, data } = await encPost('/api/admin/encryption/verify', {});
    if (!ok) { encError(data.error); return; }
    if (!data.detected) {
        encError('No valid key found in the environment yet. Set ENCRYPTION_KEY and restart, then try again.');
        return;
    }
    encEncryptNow();
}

async function encEncryptNow() {
    if (!confirm('Encrypt every stored secret and linked token now? Make sure you have saved the key first.')) return;
    const { ok, data } = await encPost('/api/admin/encryption/encrypt', {});
    if (!ok) { encError(data.error); return; }
    toast('Secrets encrypted at rest', true);
    loadEncryptionState();
}

async function encOptOut() {
    const { ok, data } = await encPost('/api/admin/encryption/opt-out', {});
    if (!ok) { encError(data.error); return; }
    loadEncryptionState();
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
    const keyInput = document.getElementById('s_' + kind + '_key');
    const key = keyInput.value.trim();
    // A blank key with one already saved is the normal case now that the field
    // can't be read back — the server falls back to the stored one.
    if (!url || !(key || keyInput.dataset.stored)) {
        toast('Enter the ' + kind + ' URL and API key first', false);
        return;
    }
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

function closeSettings() {
    stopDeviceAuthPolling();
    document.getElementById('settingsModal').classList.remove('open');
}

// ---- Filters (per viewer, not per instance) ----
// Reads and writes /api/me/prefs, not /api/settings: these belong to whoever is
// signed in, and /api/settings is admin-only. The filters are applied at read
// time against one shared calendar cache, so each account can filter the same
// cached month its own way.
async function openFilters() {
    try {
        const res = await fetch('/api/me/prefs', { cache: 'no-store' });
        const d = await res.json();
        const p = d.prefs || {};
        document.getElementById('f_genres').value = p.genres || '';
        document.getElementById('f_countries').value = p.countries || '';
        document.getElementById('f_networks').value = (p.network_filter || []).join(', ');
    } catch (e) {
        console.error(e);
        toast('Could not load your filters', false);
    }
    document.getElementById('filtersModal').classList.add('open');
}

function closeFilters() {
    document.getElementById('filtersModal').classList.remove('open');
}

// Empties the three inputs WITHOUT saving, so "Clear all" then Cancel leaves the
// stored filters alone — the same bargain every other field in these modals makes.
function clearFilters() {
    ['f_genres', 'f_countries', 'f_networks'].forEach(id => {
        document.getElementById(id).value = '';
    });
}

async function saveFilters(event) {
    event.preventDefault();
    const payload = {
        genres: document.getElementById('f_genres').value,
        countries: document.getElementById('f_countries').value,
        network_filter: document.getElementById('f_networks').value
    };
    try {
        const res = await fetch('/api/me/prefs', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const d = await res.json().catch(() => ({}));
        if (!res.ok || !d.ok) {
            toast(d.error || 'Could not save filters', false);
            return false;
        }
        // Filtering happens server-side while the month is assembled, so the
        // page has to be rebuilt to reflect it.
        window.location.reload();
    } catch (e) {
        console.error(e);
        toast('Could not save filters', false);
    }
    return false;
}

async function saveSettings(event) {
    event.preventDefault();
    const payload = {
        ...collectSecrets(),
        public_base_url: document.getElementById('s_base_url').value.trim(),
        trusted_proxy_ips: document.getElementById('s_trusted_proxies').value.trim(),
        cookie_secure: document.getElementById('s_cookie_secure').value,
        trakt_client_id: document.getElementById('s_client_id').value.trim(),
        timezone: document.getElementById('s_timezone').value.trim() || 'Europe/Athens',
        endpoint: document.getElementById('s_endpoint').value,
        pagination_limit: parseInt(document.getElementById('s_limit').value, 10) || 300,
        cache_ttl_minutes: parseInt(document.getElementById('s_cache').value, 10) || 0,
        calendar_cache_ttl_minutes: parseInt(document.getElementById('s_calcache').value, 10) || 10,
        api_cache_max_bytes: (parseInt(document.getElementById('s_cachecap').value, 10) || 1024) * MB,
        hide_not_watching: document.getElementById('s_hide').checked,
        genres: document.getElementById('s_genres').value,
        countries: document.getElementById('s_countries').value,
        network_filter: document.getElementById('s_networks').value,
        sonarr_url: document.getElementById('s_sonarr_url').value.trim(),
        sonarr_quality_profile_id: parseInt(document.getElementById('s_sonarr_qp').value, 10) || 0,
        sonarr_root_folder: document.getElementById('s_sonarr_rf').value,
        radarr_url: document.getElementById('s_radarr_url').value.trim(),
        radarr_quality_profile_id: parseInt(document.getElementById('s_radarr_qp').value, 10) || 0,
        radarr_root_folder: document.getElementById('s_radarr_rf').value,
        seer_url: document.getElementById('s_seer_url').value.trim()
    };
    try {
        const res = await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const d = await res.json().catch(() => ({}));
        if (!res.ok || !d.ok) {
            // The server validates the public base URL, so its message says what
            // is wrong with the value rather than just that the save failed.
            toast(d.error || 'Could not save settings', false);
            return false;
        }
        window.location.reload();
    } catch (e) {
        console.error(e);
        alert('⚠️ Could not save settings.');
    }
    return false;
}

// ---- Public share links (Share panel) ----
let shareState = null;
let shareSlugTimer = null;

function renderShare() {
    if (!shareState) return;
    // public_base_url is an admin-only setting, so an ordinary user can only be
    // annoyed by this, never act on it.
    document.getElementById('share_no_base_url').hidden =
        !(shareState.base_url_missing && window.IS_ADMIN);

    const kind = shareState.preferred_kind;
    document.getElementById('share_kind').value = kind;
    // Each control belongs to exactly one form: the custom name only means
    // anything for the slug link, and rotating only applies to the token.
    document.getElementById('share_slug_field').hidden = kind !== 'slug';
    document.getElementById('share_rotate').hidden = kind !== 'token';
    document.getElementById('share_slug_input').value = shareState.custom_slug || '';

    const url = shareState.urls[kind];
    const box = document.getElementById('share_url');
    box.value = url || '';
    // A slug link has nothing to resolve to until a name is saved; say which of
    // the two reasons the box is empty rather than leaving it blank.
    box.placeholder = shareState.base_url_missing
        ? 'No public base URL set'
        : (kind === 'slug' ? 'Pick a custom name above' : 'No link yet');
    renderShareView();
}

// The link's display options. A null link_view means the URL goes out bare, so
// whoever opens it sees whatever the owner's calendar currently resolves to;
// otherwise the options below are written into the query string. Neither case
// touches the owner's own view — that is the whole point of storing them here
// rather than reusing the calendar preferences.
function renderShareView() {
    const view = shareState.link_view;
    const custom = !!view;
    document.querySelector('input[name="share_view_mode"][value="current"]').checked = !custom;
    document.querySelector('input[name="share_view_mode"][value="custom"]').checked = custom;
    document.getElementById('share_view_options').hidden = !custom;
    if (!custom) return;
    if (view.endpoint) document.getElementById('share_view_endpoint').value = view.endpoint;
    if (view.tz) document.getElementById('share_view_tz').value = view.tz;
    if (view.card) document.getElementById('share_view_card').value = view.card;
    if (view.packing) document.getElementById('share_view_packing').value = view.packing;
    document.getElementById('share_view_hidenw').checked = view.hidenw === '1';
}

async function postShareView(view) {
    try {
        const res = await fetch('/api/me/share/view', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ view })
        });
        const d = await res.json();
        if (!d.ok) throw new Error(d.error || 'failed');
        shareState = d;
        renderShare();
    } catch (e) { toast('Could not update the link options', false); }
}

function setShareViewMode(mode) {
    if (mode === 'current') { postShareView(null); return; }
    // Switching to custom seeds the controls from what this page is currently
    // showing, so the first save reproduces the link the owner already had
    // rather than snapping it to some unrelated default.
    document.getElementById('share_view_options').hidden = false;
    saveShareView();
}

function saveShareView() {
    postShareView({
        endpoint: document.getElementById('share_view_endpoint').value,
        tz: document.getElementById('share_view_tz').value,
        card: document.getElementById('share_view_card').value,
        packing: document.getElementById('share_view_packing').value,
        hidenw: document.getElementById('share_view_hidenw').checked ? '1' : '0',
    });
}

async function openShare() {
    try {
        const res = await fetch('/api/me/share', { cache: 'no-store' });
        shareState = await res.json();
        renderShare();
    } catch (e) { console.error(e); }
    document.getElementById('shareModal').classList.add('open');
}

function closeShare() {
    document.getElementById('shareModal').classList.remove('open');
}

// One link at a time: this publishes the chosen form and retires the other two,
// so the dropdown is the only sharing switch there is.
async function setShareKind(kind) {
    try {
        const res = await fetch('/api/me/share/active', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ kind })
        });
        shareState = await res.json();
        renderShare();
    } catch (e) { toast('Could not update sharing', false); }
}

function checkShareSlug() {
    clearTimeout(shareSlugTimer);
    const slug = document.getElementById('share_slug_input').value.trim();
    const status = document.getElementById('share_slug_status');
    if (!slug) { status.textContent = ''; status.className = 'hint'; return; }
    status.textContent = 'Checking…';
    status.className = 'hint';
    shareSlugTimer = setTimeout(async () => {
        try {
            const res = await fetch('/api/me/share/slug-check?slug=' + encodeURIComponent(slug), { cache: 'no-store' });
            const d = await res.json();
            if (d.available) {
                status.textContent = 'Available — saving…';
                status.className = 'hint ok';
                const saveRes = await fetch('/api/me/share/slug', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ slug })
                });
                const saved = await saveRes.json();
                if (saved.ok) {
                    shareState = saved;
                    renderShare();
                    status.textContent = 'Saved';
                    status.className = 'hint ok';
                } else {
                    status.textContent = saved.error || 'Could not save';
                    status.className = 'hint err';
                }
            } else {
                status.textContent = d.error || 'Not available';
                status.className = 'hint err';
            }
        } catch (e) {
            status.textContent = 'Could not check availability';
            status.className = 'hint err';
        }
    }, 500);
}

async function rotateShareToken() {
    if (!confirm('Rotate the token link? The old link will stop working immediately.')) return;
    try {
        const res = await fetch('/api/me/share/rotate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
        shareState = await res.json();
        renderShare();
        toast('Token link rotated', true);
    } catch (e) { toast('Could not rotate token', false); }
}

function copyShareUrl() {
    const input = document.getElementById('share_url');
    if (!input || !input.value) return;
    navigator.clipboard.writeText(input.value).then(
        () => toast('Link copied', true),
        () => toast('Could not copy link', false)
    );
}

// ---- Trakt OAuth device-code authorization ----
let deviceAuthTimer = null;

function updateTokenStatus(expiresAt) {
    const el = document.getElementById('s_token_status');
    if (!el) return;
    if (!expiresAt) { el.textContent = ''; return; }
    const when = new Date(expiresAt * 1000);
    const past = when.getTime() < Date.now();
    el.textContent = past
        ? `Token expired ${when.toLocaleDateString()} — refreshing automatically, or click "Refresh token now".`
        : `Token valid until ${when.toLocaleDateString()} (refreshes automatically once it expires).`;
}

// Shows the exact redirect URI to register on the Trakt application (it has to
// match byte for byte, so showing it beats describing it), and raises the
// reconnect prompt left behind when a saved token couldn't be matched to an
// account during first-run setup.
function updateTraktLoginHints(s) {
    const field = document.getElementById('s_redirect_field');
    const input = document.getElementById('s_redirect_uri');
    if (field && input) {
        input.value = s.trakt_redirect_uri || '';
        // Nothing to register until a public base URL exists to build it from.
        field.hidden = !s.trakt_redirect_uri;
    }
    const box = document.getElementById('s_reconnect_box');
    if (box) box.hidden = !s.trakt_reconnect_notice;
    showReconnectError('');
}

function showReconnectError(message) {
    const el = document.getElementById('s_reconnect_error');
    if (!el) return;
    el.textContent = message || '';
    el.classList.toggle('warn', !!message);
}

// Retry linking the token this instance already has, and report what stopped it.
// The notice's only previous escape was the OAuth flow, which cannot help when
// the blocker is that another login here already holds the Trakt account.
async function adoptTraktToken(btn) {
    btn.disabled = true;
    showReconnectError('');
    try {
        const res = await fetch('/api/auth/trakt/adopt', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}'
        });
        const d = await res.json();
        if (d.ok) {
            document.getElementById('s_reconnect_box').hidden = true;
            toast('Trakt account linked', true);
            return;
        }
        showReconnectError(d.error || 'Could not link the saved token.');
        toast('Could not link the saved token', false);
    } catch (e) {
        console.error(e);
        showReconnectError('Could not reach the server.');
    } finally {
        btn.disabled = false;
    }
}

function copyRedirectUri() {
    const input = document.getElementById('s_redirect_uri');
    if (!input || !input.value) return;
    navigator.clipboard.writeText(input.value).then(
        () => toast('Redirect URI copied', true),
        () => toast('Could not copy', false)
    );
}

// Tells the operator what to type instead of making them work out their own
// container network, and calls out the one combination that fails silently:
// forwarded headers arriving from a peer this app doesn't trust, which collapses
// every user onto the proxy's address for rate limiting and the session list.
function updateProxyHint(s) {
    const hint = document.getElementById('s_proxy_hint');
    if (!hint) return;
    const peer = s.detected_peer_ip || 'unknown';
    let text = 'Comma-separated CIDRs whose X-Forwarded-For this app will honor. '
        + 'This request came from ' + peer + '.';
    if (s.forwarded_headers_present && !s.peer_is_trusted_proxy) {
        text += ' Forwarded headers ARE arriving but are being ignored, so every user '
            + 'currently looks like ' + peer + ' — add it here.';
        hint.classList.add('warn');
    } else {
        hint.classList.remove('warn');
    }
    hint.textContent = text;
}

// The stored policy can drift from reality — a data dir that started on
// localhost and later moved behind HTTPS keeps "never" and quietly serves
// session cookies with no Secure flag. Only the browser knows the real protocol,
// so this compares the saved value against it and warns in place, over the
// field's own explanatory text. `s` may be the settings response OR nothing (a
// re-check after the dropdown changes), so the mode is read from the control
// when not passed.
function updateCookieHint(s) {
    const select = document.getElementById('s_cookie_secure');
    const hint = document.getElementById('s_cookie_hint');
    if (!select || !hint) return;
    const mode = ((s && s.cookie_secure) || select.value || 'always').toLowerCase();
    const isHttps = window.location.protocol === 'https:';
    let text = '';
    if (mode === 'never' && isHttps) {
        text = 'You reached this page over https:// but cookies are set to "never" — '
            + 'they are going out without the Secure flag. Choose "Always".';
    } else if (mode === 'always' && !isHttps) {
        text = 'You reached this page over http://. Saving "Always" would make the session '
            + 'cookie Secure and your browser would discard it, locking you out — this is '
            + 'refused. Serve over https:// (a reverse proxy counts), or choose Auto or Never.';
    }
    hint.textContent = text || hint.dataset.base || '';
    hint.classList.toggle('warn', !!text);
}

function stopDeviceAuthPolling() {
    if (deviceAuthTimer) { clearInterval(deviceAuthTimer); deviceAuthTimer = null; }
}

// Show or hide the pairing panel, and lock the start button while a code is
// live. Re-clicking "Authorize with Trakt" used to request a FRESH code, so the
// one you had just copied stopped working — a trap, because the button looks
// exactly like the thing you press to open the page you paste the code into.
function setDeviceAuthActive(active) {
    const panel = document.getElementById('s_device_panel');
    const start = document.getElementById('s_device_start');
    if (panel) panel.hidden = !active;
    if (start) {
        start.disabled = active;
        start.textContent = active ? '⏳ Waiting for approval…' : '🔑 Authorize with Trakt';
    }
}

function copyDeviceCode() {
    const input = document.getElementById('s_device_code');
    if (!input || !input.value) return;
    navigator.clipboard.writeText(input.value).then(
        () => toast('Pairing code copied', true),
        () => toast('Could not copy — select it and copy by hand', false)
    );
}

function cancelDeviceAuth() {
    stopDeviceAuthPolling();
    setDeviceAuthActive(false);
    const box = document.getElementById('authStatus');
    if (box) box.textContent = 'Authorization cancelled.';
}

async function startDeviceAuth() {
    stopDeviceAuthPolling();
    const clientId = document.getElementById('s_client_id').value.trim();
    const secretInput = document.getElementById('s_client_secret');
    const clientSecret = secretInput.value.trim();
    const box = document.getElementById('authStatus');
    if (!clientId) { toast('Enter your Trakt Client ID first', false); return; }
    // Blank is fine when one is already saved; the poll endpoint falls back to it.
    if (!clientSecret && !secretInput.dataset.stored) {
        toast('Enter your Trakt Client Secret first', false);
        return;
    }
    box.textContent = 'Requesting a device code…';
    try {
        const res = await fetch('/api/auth/device/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ client_id: clientId })
        });
        const d = await res.json();
        if (!d.ok) { box.textContent = d.error || 'Could not start authorization.'; toast(d.error || 'Could not start authorization', false); return; }
        document.getElementById('s_device_code').value = d.user_code || '';
        const link = document.getElementById('s_device_link');
        link.href = d.verification_url || 'https://trakt.tv/activate';
        setDeviceAuthActive(true);
        box.textContent = 'Copy the code, open trakt.tv, and enter it there. Waiting for approval…';
        const deadline = Date.now() + (d.expires_in || 600) * 1000;
        const intervalMs = Math.max(d.interval || 5, 5) * 1000;
        deviceAuthTimer = setInterval(
            () => pollDeviceAuth(d.device_code, clientId, clientSecret, deadline),
            intervalMs
        );
    } catch (e) {
        console.error(e);
        box.textContent = 'Could not start authorization.';
    }
}

async function pollDeviceAuth(deviceCode, clientId, clientSecret, deadline) {
    const box = document.getElementById('authStatus');
    if (Date.now() > deadline) {
        stopDeviceAuthPolling();
        setDeviceAuthActive(false);
        box.textContent = 'The code expired before it was approved — try again.';
        return;
    }
    try {
        const res = await fetch('/api/auth/device/poll', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ device_code: deviceCode, client_id: clientId, client_secret: clientSecret })
        });
        const d = await res.json();
        if (d.status === 'pending' || d.status === 'slow_down') return;  // keep waiting
        stopDeviceAuthPolling();
        setDeviceAuthActive(false);
        if (d.status === 'authorized') {
            // The token isn't sent back — it is already saved server-side, and
            // putting a bearer token in page memory would serve no purpose.
            const tokenInput = document.getElementById('s_access_token');
            tokenInput.value = '';
            tokenInput.dataset.stored = '1';
            tokenInput.placeholder = 'Saved — leave blank to keep it';
            updateTokenStatus(d.expires_at);
            // The server also tries to adopt the fresh token as this admin's own
            // linked identity, which is what takes the reconnect notice down.
            // When that part fails it says why: an authorization that "worked"
            // yet left the notice up is the state that reads as the app ignoring
            // what you just did.
            if (d.trakt_linked) {
                const notice = document.getElementById('s_reconnect_box');
                if (notice) notice.hidden = true;
                box.textContent = '✅ Authorized, and linked to your account.';
            } else if (d.trakt_link_error) {
                box.textContent = '✅ Authorized, but not linked to your login: ' + d.trakt_link_error;
                showReconnectError(d.trakt_link_error);
            } else {
                box.textContent = '✅ Authorized! The access token has been saved.';
            }
            toast('Trakt authorized', true);
        } else {
            box.textContent = d.error || 'Authorization failed.';
            toast(d.error || 'Authorization failed', false);
        }
    } catch (e) {
        console.error(e);  // transient network hiccup; keep polling until the deadline
    }
}

async function refreshTraktToken() {
    const box = document.getElementById('authStatus');
    try {
        // Body-less, but still declared JSON: every mutating request in this app
        // has to be, or it is refused before it reaches the handler.
        const res = await fetch('/api/auth/refresh', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}'
        });
        const d = await res.json();
        if (!d.ok) { toast(d.error || 'Refresh failed', false); if (box) box.textContent = d.error || ''; return; }
        updateTokenStatus(d.expires_at);
        toast('Trakt token refreshed', true);
    } catch (e) {
        console.error(e);
        toast('Refresh failed', false);
    }
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
    if (!window.IS_ADMIN) return;
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
            ? `<div class="trailer-embed"><iframe src="https://www.youtube-nocookie.com/embed/${yt}" title="Trailer" loading="lazy" referrerpolicy="strict-origin-when-cross-origin" allow="accelerometer; encrypted-media; gyroscope; picture-in-picture" allowfullscreen></iframe></div>`
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

// Escape closes whichever overlay is open. Driven off the class rather than a
// list of close functions: the Settings modal is only rendered for admins, so
// calling closeSettings() unconditionally threw for everybody else — which took
// the rest of the handler down with it and left Escape doing nothing at all.
document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    stopDeviceAuthPolling();
    document.querySelectorAll('.modal-overlay.open').forEach(m => m.classList.remove('open'));
});

// ---- Hidden /distrakt reveal: Konami code + footer build-tap (kept independent) ----
// Both unlocks only lead anywhere for an account the server would actually let
// into the tracker. For everyone else the Konami code plays the sound and stops
// there, which reads as a self-contained joke rather than a locked door, and the
// footer tap does nothing at all — audio from a stray tap on a version number
// would startle someone who wasn't looking for an easter egg and give the game
// away in the process.
function revealSecret() {
    if (!window.DISTRAKT_AVAILABLE) return;
    // Remember that the easter egg has been used so the calendar can surface a
    // permanent Distrakt nav button on future visits.
    try { localStorage.setItem('distraktRevealed', '1'); } catch (e) {}
    location.href = '/distrakt';
}

// Once revealed, show the Distrakt nav button on the calendar.
document.addEventListener('DOMContentLoaded', () => {
    if (!window.DISTRAKT_AVAILABLE) return;
    let revealed = false;
    try { revealed = localStorage.getItem('distraktRevealed') === '1'; } catch (e) {}
    const nav = document.getElementById('distraktNav');
    if (revealed && nav) nav.hidden = false;
});

const KONAMI_SEQUENCE = ['ArrowUp', 'ArrowUp', 'ArrowDown', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'ArrowLeft', 'ArrowRight', 'b', 'a'];
let konamiBuffer = [];

document.addEventListener('keydown', (e) => {
    konamiBuffer.push(e.key);
    konamiBuffer = konamiBuffer.slice(-KONAMI_SEQUENCE.length);
    if (konamiBuffer.length === KONAMI_SEQUENCE.length && konamiBuffer.every((k, i) => k === KONAMI_SEQUENCE[i])) {
        konamiBuffer = [];
        if (window.DISTRAKT_AVAILABLE) revealSecret();
        else new Audio('/static/audio/distrakt.mp3').play().catch(() => {});
    }
});

const BUILD_TAP_TARGET = 7;
const BUILD_TAP_WINDOW_MS = 1500;
let buildTapCount = 0;
let buildTapLast = 0;

document.addEventListener('DOMContentLoaded', () => {
    if (!window.DISTRAKT_AVAILABLE) return;
    const tag = document.querySelector('.version-tag');
    if (!tag) return;
    tag.addEventListener('click', () => {
        const now = Date.now();
        buildTapCount = (now - buildTapLast > BUILD_TAP_WINDOW_MS) ? 1 : buildTapCount + 1;
        buildTapLast = now;
        if (buildTapCount >= BUILD_TAP_TARGET) revealSecret();
    });
});
