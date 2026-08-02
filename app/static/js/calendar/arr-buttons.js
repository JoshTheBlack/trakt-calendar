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

// The two appliers below RE-RENDER FROM STATE ALREADY IN MEMORY — no request —
// so they are cheap to run again over one newly arrived subtree. `root` is what
// to walk: the whole document after a refresh, or just the block that landed.
function applyLibraryStatus(root = document) {
    root.querySelectorAll('.arr-btn').forEach(btn => {
        if (btn.dataset.busy === '1') return;
        const kind = btn.dataset.arr;
        const card = btn.closest('.card');
        const id = libIdFor(kind, card ? card.dataset : btn.dataset);
        if (id && libraryIds[kind] && libraryIds[kind].has(String(id))) {
            markInLibrary(btn, 'Already in ' + kind.charAt(0).toUpperCase() + kind.slice(1));
        }
    });
}

function applyArrStatus(root = document) {
    root.querySelectorAll('.arr-btn').forEach(btn => {
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
            // `el` is not always the calendar tile's own button: the details modal
            // (buildDetailsActions in details-modal.js) builds a SEPARATE arr-btn
            // for the same title, so marking only `el` leaves the tile behind the
            // modal showing a plain Add button until the next 60s poll or a full
            // refresh. applyLibraryStatus() re-renders every arr-btn on the page
            // from the libraryIds state just updated above — no request, so it is
            // cheap to run again for one add.
            applyLibraryStatus();
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
async function addAllToArr(event) {
    // Only the Sonarr/Radarr buttons (not Seerr) — this month's endpoint is TV-only or movie-only.
    const btns = [...document.querySelectorAll('.card:not(.not-watching) .arr-btn')]
        .filter(b => b.dataset.arr !== 'seer' && !b.disabled && b.dataset.added !== '1' && b.dataset.busy !== '1');
    if (!btns.length) {
        toast('Nothing to add — check items are watching and Sonarr/Radarr are reachable.', false);
        return;
    }
    confirmInline(event.currentTarget,
        `Add ${btns.length} watching item${btns.length === 1 ? '' : 's'} to your library?`,
        async () => {
            toast(`Adding ${btns.length} item${btns.length === 1 ? '' : 's'}…`, true);
            let i = 0;
            const worker = async () => { while (i < btns.length) { await addToArr(btns[i++]); } };
            await Promise.all([worker(), worker(), worker()]);  // 3 concurrent
        });
}

// Cards that arrived after the page initialised — a day block that fetched
// itself in as it was scrolled to. Its buttons are laid out by the same template
// as the shell's, but nothing had visited them: both appliers walk from a root,
// and between an insertion and the next 60s poll tick that root was never the
// document. So a late day showed plain Add buttons for up to a minute, whether
// or not the title was already in the library. Marked from the status and
// library sets this page already holds, which is why it costs no request.
function applyArrStateTo(root) {
    if (!window.IS_ADMIN || !root || !root.querySelectorAll) return;
    applyArrStatus(root);
    applyLibraryStatus(root);
}

// The add/request buttons and the health state behind them only exist for an
// administrator, so nobody else polls for them. Re-run on each (re)init so cards
// swapped in by a boosted nav get their in-library / reachability marks; the 60s
// poll is armed only once, since a boosted nav must not stack a second interval.
let arrPollArmed = false;
function initArrIntegrations() {
    if (!window.IS_ADMIN) return;
    refreshArrStatus();
    refreshLibrary();
    if (!arrPollArmed) {
        arrPollArmed = true;
        setInterval(() => { refreshArrStatus(); refreshLibrary(); }, 60000);
    }
}
