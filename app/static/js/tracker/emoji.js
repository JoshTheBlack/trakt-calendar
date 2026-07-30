// The network -> emoji map: the fallback every row uses when a network has no
// cached logo, and the editor that maintains it.

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

// ---- Network -> emoji map editor ----
// The map belongs to THIS account and decides how ITS Discord posts render, so
// every tracker user edits their own. It used to be saved through the admin-only
// settings endpoint, which meant one shared map and only an administrator able
// to change what anybody's posts looked like.
async function loadEmojiMap() {
    try {
        const res = await fetch('/api/distrakt/emojis', { cache: 'no-store' });
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
        const res = await fetch('/api/distrakt/emojis', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ network_emojis: map, default_network_emoji: newDefault })
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
