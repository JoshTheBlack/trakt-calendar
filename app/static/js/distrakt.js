// Distrakt page logic: add-show flow, bucketed month show list, abandon toggle,
// the network->emoji map editor (CHAT 3), and the bucketed list + POST 1/POST 2
// copy blocks sourced from GET /api/distrakt/month (CHAT 4).

function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// Same toast() as app.js (kept local — distrakt.js is deliberately standalone, per BUILD_PLAN §7).
function toast(message, ok) {
    let host = document.getElementById('toastHost');
    if (!host) { host = document.createElement('div'); host.id = 'toastHost'; document.body.appendChild(host); }
    const t = document.createElement('div');
    t.className = 'toast ' + (ok ? 'ok' : 'err');
    t.textContent = message;
    host.appendChild(t);
    while (host.children.length > 6) host.firstChild.remove();
    requestAnimationFrame(() => t.classList.add('show'));
    setTimeout(() => { t.classList.remove('show'); setTimeout(() => t.remove(), 300); }, 4200);
}

// Seeded from the page, which renders the app-wide map server-side. Only an
// admin can re-read or edit it through the settings endpoint, but everyone
// viewing this page needs the emoji to fall back to when a network has no logo.
let networkEmojis = window.NETWORK_EMOJIS || {};
let defaultEmoji = window.DEFAULT_NETWORK_EMOJI || ':tv:';
let emojiEntries = [];

function emojiFor(network) {
    return networkEmojis[network] || defaultEmoji;
}

// A network-logo <img> that 404s (no cached TMDB logo) falls back to its emoji.
function onLogoError(img) {
    const span = img.parentElement;
    if (span) span.textContent = img.getAttribute('data-emoji') || '';
}

// ---- Bucketed month data: shows (with live x/y + bucket) + POST 1/POST 2 ----
const BUCKET_LABELS = {
    cleanup: 'Cleanup', keepup: 'Keepup', new: 'New Shows', returning: 'Returning',
    completed: 'Completed', abandoned: 'Abandoned',
};
const BUCKET_ORDER = ['cleanup', 'keepup', 'new', 'returning', 'completed', 'abandoned'];
const WEEKDAY_ORDER = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

let monthData = null;
let monthClosed = false;  // true whenever the month is read-only (frozen OR never-tracked past)
let networkTmdb = {};     // network name -> a tmdb id (from the roster), for logo gen/regen

function applyMonthResponse(d) {
    monthData = d;
    // A frozen past month (d.closed) and a never-tracked past month (d.readonly,
    // blocked by §6 no-backfill) are both read-only — hide the add/edit controls.
    monthClosed = !!d.closed || !!d.readonly;
    // Build network -> tmdb from the roster so the emoji-map logos can generate/regen.
    networkTmdb = {};
    (d.shows || []).forEach(s => { if (s.network && s.tmdb) networkTmdb[s.network] = s.tmdb; });
    applyReadonlyState(monthClosed, d.closed ? 'frozen' : (d.readonly ? 'untracked' : ''));
    renderShowList(d.shows || []);
    renderCopyBlocks(d.post1 || '', d.post2 || '');
    if (emojiEntries.length) renderEmojiRows();  // refresh emoji-row logos now we have tmdb
}

async function loadMonthData() {
    const host = document.getElementById('distraktShowList');
    host.innerHTML = '<div class="distrakt-empty">Loading…</div>';
    try {
        const res = await fetch(`/api/distrakt/month?year=${window.DISTRAKT_YEAR}&month=${window.DISTRAKT_MONTH}`);
        const d = await res.json();
        if (!d.ok) throw new Error(d.error || 'failed');
        applyMonthResponse(d);
    } catch (e) {
        host.innerHTML = '<div class="distrakt-empty">Could not load shows.</div>';
        renderCopyBlocks('', '');
    }
}

// Force a fresh totals refresh (§3): POST /api/distrakt/refresh bypasses the 24h
// season cache and re-stamps totals_refreshed_at. Past/closed months are frozen,
// so the server simply returns the snapshot unchanged.
async function refreshMonth() {
    const host = document.getElementById('distraktShowList');
    host.innerHTML = '<div class="distrakt-empty">Refreshing…</div>';
    try {
        const res = await fetch('/api/distrakt/refresh', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ year: window.DISTRAKT_YEAR, month: window.DISTRAKT_MONTH })
        });
        const d = await res.json();
        if (!d.ok) throw new Error(d.error || 'failed');
        applyMonthResponse(d);
        toast((d.closed || d.readonly) ? 'Past month (read-only)' : 'Refreshed totals', true);
    } catch (e) {
        toast('Could not refresh', false);
        loadMonthData();
    }
}

// Pull this month's calendar premieres into the open month (New/Returning),
// skipping shows already present or toggled not-watching. Use it to seed the
// current month when its doc already exists (lazy-init only seeds premieres once).
async function importFromCalendar() {
    const host = document.getElementById('distraktShowList');
    host.innerHTML = '<div class="distrakt-empty">Importing premieres…</div>';
    try {
        const res = await fetch('/api/distrakt/import', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ year: window.DISTRAKT_YEAR, month: window.DISTRAKT_MONTH })
        });
        const d = await res.json();
        if (!d.ok) throw new Error(d.error || 'failed');
        applyMonthResponse(d);
        toast('Imported premieres from calendar', true);
    } catch (e) {
        toast('Could not import from calendar', false);
        loadMonthData();
    }
}

// Delete a show from the tracker entirely (cleanup mistakes, incl. abandoned ones).
async function deleteShow(traktId, season) {
    if (!confirm('Remove this show from the tracker for this month? This cannot be undone.')) return;
    try {
        const res = await fetch('/api/distrakt/remove', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ year: window.DISTRAKT_YEAR, month: window.DISTRAKT_MONTH, trakt_id: traktId, season })
        });
        const d = await res.json();
        if (!d.ok) throw new Error(d.error || 'failed');
        toast('Removed from tracker', true);
        applyMonthResponse(d);  // mutation returns the recomputed month (1d)
    } catch (e) {
        toast('Could not remove show', false);
    }
}

// Read-only months hide the add/edit affordances and show a banner (abandon
// buttons are also omitted per-row, see showRow). `kind` picks the message:
// 'frozen' = a closed snapshot, 'untracked' = a never-tracked past month (§6).
function applyReadonlyState(readonly, kind) {
    const toolbar = document.querySelector('.distrakt-actions');
    if (toolbar) toolbar.style.visibility = readonly ? 'hidden' : '';
    let note = document.getElementById('distraktFrozenNote');
    if (readonly) {
        const text = kind === 'untracked'
            ? '🕗 Past month — not tracked (read-only). Months earlier than your first tracked month are never backfilled.'
            : '🔒 Past month — frozen snapshot (read-only).';
        if (!note) {
            note = document.createElement('div');
            note.id = 'distraktFrozenNote';
            note.className = 'distrakt-frozen-note';
            const main = document.querySelector('.distrakt-main');
            if (main) main.prepend(note);
        }
        note.textContent = text;
    } else if (note) {
        note.remove();
    }
}

function renderCopyBlocks(post1, post2) {
    document.getElementById('post1Text').value = post1;
    document.getElementById('post2Text').value = post2;
}

// ---- The share link embedded in POST 1 ----
// The link itself is rendered into post1 server-side; these two selectors only
// choose WHICH link and which view, then reload the month so the copy block
// reflects the change immediately.
const LINK_KIND_LABELS = { token: 'Private link', username: 'Username link', slug: 'Custom link' };

function renderPostLink(d) {
    const kindSel = document.getElementById('postLinkKind');
    const endpointSel = document.getElementById('postLinkEndpoint');
    if (!kindSel || !endpointSel) return;
    // Only forms that would actually resolve are offered — a selector entry that
    // silently produces no link is worse than not being there.
    const options = [`<option value="">Link: as shared (${LINK_KIND_LABELS[d.preferred_kind] || d.preferred_kind})</option>`];
    Object.keys(LINK_KIND_LABELS).forEach(kind => {
        if (d.available[kind]) options.push(`<option value="${kind}">Link: ${LINK_KIND_LABELS[kind]}</option>`);
    });
    kindSel.innerHTML = options.join('');
    kindSel.value = d.kind || '';
    endpointSel.value = d.endpoint || '';
    // Nothing publishable at all (no public base URL configured, or every form
    // switched off): the post omits the link, so hide the controls that pick it.
    const usable = !d.base_url_missing && Object.values(d.available).some(Boolean);
    kindSel.hidden = !usable;
    endpointSel.hidden = !usable;
}

async function loadPostLink() {
    try {
        const res = await fetch('/api/distrakt/share-link');
        const d = await res.json();
        if (d.ok) renderPostLink(d);
    } catch (e) { /* the copy block still works without the selectors */ }
}

async function savePostLink() {
    const kindSel = document.getElementById('postLinkKind');
    const endpointSel = document.getElementById('postLinkEndpoint');
    try {
        const res = await fetch('/api/distrakt/share-link', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ kind: kindSel.value, endpoint: endpointSel.value }),
        });
        const d = await res.json();
        if (!d.ok) throw new Error(d.error || 'failed');
        renderPostLink(d);
        loadMonthData();
    } catch (e) {
        toast('Could not save the post link', false);
    }
}

async function copyBlock(which) {
    const el = document.getElementById(which === 'post1' ? 'post1Text' : 'post2Text');
    try {
        await navigator.clipboard.writeText(el.value);
        toast('Copied to clipboard', true);
    } catch (e) {
        el.select();
        toast('Could not copy — text selected instead', false);
    }
}

// Alphabetical sort key ignoring a leading article (mirrors discord_fmt._sort_title).
function sortTitle(t) {
    const s = String(t || '').trim().toLowerCase();
    const m = s.match(/^(the|a|an)\s+(.*)$/);
    return m ? m[2] : s;
}
const byTitle = (a, b) => sortTitle(a.title).localeCompare(sortTitle(b.title));
// New Shows / Returning order by release (premiere) date, ties by title.
function premiereKey(s) {
    const p = String(s.premiere || '').split('/');
    return (parseInt(p[0], 10) || 99) * 100 + (parseInt(p[1], 10) || 99);
}
const byPremiere = (a, b) => (premiereKey(a) - premiereKey(b)) || byTitle(a, b);

function renderShowList(shows) {
    const host = document.getElementById('distraktShowList');
    if (!shows.length) {
        host.innerHTML = '<div class="distrakt-empty">No shows tracked yet this month.</div>';
        return;
    }
    const groups = {};
    BUCKET_ORDER.forEach(b => groups[b] = []);
    shows.forEach(s => (groups[s.bucket] || (groups[s.bucket] = [])).push(s));

    let html = '';
    BUCKET_ORDER.forEach(bucket => {
        const rows = groups[bucket] || [];
        if (!rows.length) return;
        // New/Returning by release date; everything else alphabetical.
        const cmp = (bucket === 'new' || bucket === 'returning') ? byPremiere : byTitle;
        html += `<div class="distrakt-bucket-head">${esc(BUCKET_LABELS[bucket] || bucket)}</div>`;
        if (bucket === 'keepup') {
            const byDay = {};
            WEEKDAY_ORDER.forEach(d => byDay[d] = []);
            rows.forEach(s => (byDay[s.cadence] || (byDay[s.cadence] = [])).push(s));
            WEEKDAY_ORDER.forEach(day => {
                const dayRows = byDay[day] || [];
                if (!dayRows.length) return;
                html += `<div class="distrakt-weekday-head">${esc(day)}</div>`;
                html += dayRows.sort(byTitle).map(showRow).join('');
            });
        } else {
            html += rows.slice().sort(cmp).map(showRow).join('');
        }
    });
    host.innerHTML = html;
    setupTitleScroll(host);
}

function showRow(s) {
    const isNewRet = s.bucket === 'new' || s.bucket === 'returning';
    const counts = isNewRet ? `${s.watched}/${s.total}${s.cadence ? ', ' + s.cadence : ''}`
        : (s.bucket === 'completed') ? '' : `${s.watched}/${s.total}`;
    // New/Returning: premiere (– finale for weekly). Keepup: finale (end date).
    let dates = '';
    if (isNewRet) dates = (s.cadence === 'b') ? (s.premiere || '?/?') : `${s.premiere || '?/?'} – ${s.finale || '?/?'}`;
    else if (s.bucket === 'keepup') dates = s.finale || '?/?';
    const actions = monthClosed ? '' : `
            <button type="button" class="btn-ghost small" onclick="toggleAbandon(${s.trakt_id}, ${s.season}, ${!s.abandoned})">${s.abandoned ? 'Un-abandon' : 'Abandon'}</button>
            <button type="button" class="btn-ghost small danger" onclick="deleteShow(${s.trakt_id}, ${s.season})" title="Remove from tracker">✕</button>`;
    const net = s.network || '';
    // Prefer the TMDB network logo (shared cache with the calendar); if it isn't
    // cached (404) fall back to the mapped emoji token.
    const badge = net
        ? `<img class="distrakt-logo" src="/api/network-logo?name=${encodeURIComponent(net)}&tmdb=${s.tmdb || ''}" alt="" data-emoji="${esc(emojiFor(net))}" onerror="onLogoError(this)">`
        : esc(emojiFor(net));
    return `
        <div class="distrakt-show-row${s.abandoned ? ' abandoned' : ''}" title="${esc(net)}">
            <span class="distrakt-badge">${badge}</span>
            <span class="distrakt-title"><span class="tt">${esc(s.title)}</span></span>
            <span class="distrakt-season">S${String(s.season).padStart(2, '0')}</span>
            <span class="distrakt-counts">${counts ? '(' + esc(counts) + ')' : ''}</span>
            <span class="distrakt-dates">${esc(dates)}</span>
            <span class="distrakt-row-actions">${actions}</span>
        </div>`;
}

// Marquee-scroll a title on hover only when it actually overflows its cell.
function setupTitleScroll(host) {
    host.querySelectorAll('.distrakt-title').forEach(cell => {
        const inner = cell.querySelector('.tt');
        if (!inner) return;
        cell.addEventListener('mouseenter', () => {
            const overflow = inner.scrollWidth - cell.clientWidth;
            if (overflow > 4) {
                inner.style.setProperty('--scroll', overflow + 'px');
                inner.style.setProperty('--dur', Math.max(2.5, overflow / 45) + 's');
                cell.classList.add('scrolling');
            }
        });
        cell.addEventListener('mouseleave', () => {
            cell.classList.remove('scrolling');
            inner.style.removeProperty('--scroll');
        });
    });
}

async function toggleAbandon(traktId, season, abandoned) {
    try {
        const res = await fetch('/api/distrakt/abandon', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ year: window.DISTRAKT_YEAR, month: window.DISTRAKT_MONTH, trakt_id: traktId, season, abandoned })
        });
        const d = await res.json();
        if (!d.ok) throw new Error(d.error || 'failed');
        toast(abandoned ? 'Marked abandoned' : 'Un-abandoned', true);
        applyMonthResponse(d);  // mutation returns the recomputed month (1d)
    } catch (e) {
        toast('Could not update abandon status', false);
    }
}

// ---- Add-show modal: search -> pick show -> pick season -> POST add ----
let searchTimer = null;
let searchResults = [];
let pickedShow = null;

function openAddShow() {
    document.getElementById('addSearchInput').value = '';
    document.getElementById('addSearchResults').innerHTML = '';
    document.getElementById('addSeasonPick').hidden = true;
    pickedShow = null;
    document.getElementById('addShowModal').classList.add('open');
    document.getElementById('addSearchInput').focus();
}

function closeAddShow() {
    document.getElementById('addShowModal').classList.remove('open');
}

function onAddSearchInput() {
    clearTimeout(searchTimer);
    const q = document.getElementById('addSearchInput').value.trim();
    document.getElementById('addSeasonPick').hidden = true;
    if (!q) { document.getElementById('addSearchResults').innerHTML = ''; return; }
    searchTimer = setTimeout(() => runAddSearch(q), 300);
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
    const url = `/api/distrakt/seasons?id=${encodeURIComponent(pickedShow.trakt_id)}`;
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
    try {
        const res = await fetch('/api/distrakt/add', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                year: window.DISTRAKT_YEAR, month: window.DISTRAKT_MONTH,
                trakt_id: pickedShow.trakt_id, tmdb: pickedShow.tmdb, slug: pickedShow.slug,
                title: pickedShow.title, network: pickedShow.network, season
            })
        });
        const d = await res.json();
        if (!d.ok) throw new Error(d.error || 'failed');
        toast(`Added ${pickedShow.title} S${String(season).padStart(2, '0')}`, true);
        closeAddShow();
        applyMonthResponse(d);  // mutation returns the recomputed month (1d)
    } catch (e) {
        toast('Could not add show', false);
    }
}

// ---- Network -> emoji map editor (saves via the existing POST /api/settings) ----
// Admin-only: the map is app-wide configuration and the settings endpoint that
// backs it is gated. Its tab and panel aren't rendered for anyone else.
async function loadEmojiMap() {
    if (!window.IS_ADMIN) return;
    try {
        const res = await fetch('/api/settings', { cache: 'no-store' });
        const s = await res.json();
        networkEmojis = s.network_emojis || {};
        defaultEmoji = s.default_network_emoji || ':tv:';
        document.getElementById('e_default').value = defaultEmoji;
        emojiEntries = Object.entries(networkEmojis);
        renderEmojiRows();
    } catch (e) { console.error(e); }
}

function renderEmojiRows() {
    const host = document.getElementById('emojiRows');
    // Alphabetize by network name.
    emojiEntries.sort((a, b) => String(a[0] || '').toLowerCase().localeCompare(String(b[0] || '').toLowerCase()));
    host.innerHTML = emojiEntries.map(([network, emoji], i) => {
        const nm = encodeURIComponent(network || '');
        const tm = networkTmdb[network] || '';
        const logo = network
            ? `<img class="emoji-logo" src="/api/network-logo?name=${nm}&tmdb=${tm}" alt="" onload="onEmojiLogoLoad(this)" onerror="onEmojiLogoError(this)">`
            : '';
        return `
        <div class="emoji-row">
            <span class="emoji-logo-cell">${logo}</span>
            <input type="text" value="${esc(network)}" placeholder="Network name" data-role="network" data-i="${i}">
            <input type="text" value="${esc(emoji)}" placeholder=":emoji:" data-role="emoji" data-i="${i}">
            <span class="logo-actions">
                <a class="btn-ghost small" href="/api/network-logo?name=${nm}&download=1" download title="Download logo PNG">⬇</a>
                <button type="button" class="btn-ghost small" data-net="${esc(network)}" onclick="regenLogo(this)" title="Regenerate logo">↻</button>
            </span>
            <button type="button" class="btn-ghost small" onclick="removeEmojiRow(${i})">Remove</button>
        </div>`;
    }).join('');
}

function onEmojiLogoLoad(img) { const r = img.closest('.emoji-row'); if (r) r.classList.add('has-logo'); }
function onEmojiLogoError(img) { const r = img.closest('.emoji-row'); if (r) r.classList.remove('has-logo'); img.style.display = 'none'; }

// Regenerate a single network's logo (clear cache + re-resolve from TMDB), then
// reload its <img> with a cache-buster.
async function regenLogo(btn) {
    const network = btn.dataset.net;
    if (!network) return;
    btn.disabled = true;
    try {
        const res = await fetch('/api/network-logo/regenerate', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: network, tmdb: networkTmdb[network] || '' })
        });
        const d = await res.json();
        if (!d.ok) throw new Error(d.error || 'failed');
        const row = btn.closest('.emoji-row');
        const img = row && row.querySelector('.emoji-logo');
        if (img) {
            img.style.display = '';
            img.src = `/api/network-logo?name=${encodeURIComponent(network)}&tmdb=${networkTmdb[network] || ''}&t=${Date.now()}`;
        }
        toast(d.generated ? `Regenerated ${network} logo` : `No TMDB logo found for ${network}`, d.generated);
    } catch (e) {
        toast('Could not regenerate logo', false);
    } finally {
        btn.disabled = false;
    }
}

// Add every network used by this month's shows into the map (preserving unsaved edits).
async function backfillNetworks() {
    _syncEmojiEntriesFromDom();
    try {
        const res = await fetch('/api/distrakt/backfill-networks', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ year: window.DISTRAKT_YEAR, month: window.DISTRAKT_MONTH })
        });
        const d = await res.json();
        if (!d.ok) throw new Error('failed');
        const have = new Set(emojiEntries.map(e => e[0]));
        Object.keys(d.network_emojis || {}).forEach(net => {
            if (!have.has(net)) emojiEntries.push([net, d.default_network_emoji || defaultEmoji]);
        });
        renderEmojiRows();
        toast('Backfilled networks from shows', true);
    } catch (e) {
        toast('Could not backfill networks', false);
    }
}

// Read whatever's currently in the DOM back into emojiEntries before any
// add/remove re-render — otherwise unsaved edits get clobbered by the stale
// (last-loaded-or-saved) array (was the "+ Add network" reload bug).
function _syncEmojiEntriesFromDom() {
    const rows = [...document.querySelectorAll('#emojiRows .emoji-row')];
    emojiEntries = rows.map(row => [
        row.querySelector('[data-role="network"]').value,
        row.querySelector('[data-role="emoji"]').value,
    ]);
}

function addEmojiRow() {
    _syncEmojiEntriesFromDom();
    emojiEntries.push(['', '']);
    renderEmojiRows();
}

function removeEmojiRow(i) {
    _syncEmojiEntriesFromDom();
    emojiEntries.splice(i, 1);
    renderEmojiRows();
}

async function saveEmojiMap() {
    const rows = [...document.querySelectorAll('#emojiRows .emoji-row')];
    const map = {};
    rows.forEach(row => {
        const network = row.querySelector('[data-role="network"]').value.trim();
        const emoji = row.querySelector('[data-role="emoji"]').value.trim();
        if (network) map[network] = emoji;
    });
    const newDefault = document.getElementById('e_default').value.trim() || ':tv:';
    try {
        const res = await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ network_emojis: JSON.stringify(map), default_network_emoji: newDefault })
        });
        const d = await res.json();
        if (!d.ok) throw new Error('save failed');
        networkEmojis = map;
        defaultEmoji = newDefault;
        emojiEntries = Object.entries(networkEmojis);
        toast('Emoji map saved', true);
        loadMonthData();
    } catch (e) {
        toast('Could not save emoji map', false);
    }
}

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

document.addEventListener('DOMContentLoaded', async () => {
    await loadEmojiMap();
    loadPostLink();
    loadMonthData();
});
