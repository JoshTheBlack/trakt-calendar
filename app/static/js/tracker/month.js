// The month this page is showing: fetching it, re-fetching it, and the two
// states it can be in that the controls have to respect.
//
// Every mutation route returns the recomputed month, so applyMonthResponse is
// the single place a new payload lands however it arrived — first load, a
// refresh, or a row being removed.

let monthData = null;
let monthClosed = false;  // true whenever the month is read-only (frozen OR never-tracked past)
let networkTmdb = {};     // network name -> a tmdb id (from the roster), for logo gen/regen

function applyMonthResponse(d) {
    monthData = d;
    // A frozen past month (d.closed) and a never-tracked past month (d.readonly —
    // the tracker only rolls forward and never backfills a month nobody was
    // tracking at the time) are both read-only — hide the add/edit controls.
    monthClosed = !!d.closed || !!d.readonly;
    // Build network -> tmdb from the roster so the emoji-map logos can generate/regen.
    networkTmdb = {};
    (d.shows || []).forEach(s => { const tmdb = (s.ids || {}).tmdb; if (s.network && tmdb) networkTmdb[s.network] = tmdb; });
    applyReadonlyState(monthClosed, d.closed ? 'frozen' : (d.readonly ? 'untracked' : ''));
    renderNotice(d);
    renderShowList(d.shows || [], d.movies || [], d.empty_note || '',
                   d.unknown_episodes || [], d.given_up_episodes || [],
                   d.unbacked_verdicts || []);
    renderCopyBlocks(d.post1 || '', d.post2 || '');
    if (emojiEntries.length) renderEmojiRows();  // refresh emoji-row logos now we have tmdb
    if (d.highlight) { pointAtRow(d.highlight); }
}

// How long the mark stays up. Long enough to be seen after the scroll settles,
// short enough that it is gone before the reader starts using the page — it says
// "this is the one you just added", which stops being worth saying almost at once.
const ROW_MARK_MS = 2600;

// WHICH ROW A MUTATION JUST WROTE, scrolled to and marked.
//
// WHY THE PAGE DOES NOT WORK IT OUT ITSELF. A season posted as ids-plus-a-number
// becomes a row under a key the SERVER derives, by a rule about which of a
// title's ids wins; re-deriving that here would be a second copy of it in a
// second language, wrong for exactly the co-tracked titles the first one exists
// to get right. So the route says which row it wrote and this finds it.
//
// NOT FOUND IS AN ORDINARY OUTCOME AND NOT A FAILURE. An add can settle onto a
// month this page is not showing, and a film added to a closed month lands in a
// snapshot rather than here — so there is often nothing to point at, and the
// honest response is to draw nothing rather than to report a problem.
//
// THE LIST IS ALREADY DRAWN when this runs — renderShowList sets innerHTML
// synchronously — so there is none of the calendar's waiting for a day block to
// arrive. What CAN still move it is a network logo loading and resizing a row
// above it, which is why the mark is added first and the scroll is re-issued
// once on the next frame rather than being chased.
function pointAtRow(target) {
    const key = String((target && target.key) || '');
    const season = String((target && target.season) !== undefined ? target.season : '');
    if (!key) { return; }
    const row = document.querySelector(
        `#distraktShowList .distrakt-show-row[data-key="${CSS.escape(key)}"]` +
        `[data-season="${CSS.escape(season)}"]`);
    if (!row) { return; }
    row.classList.add('row-marked');
    row.scrollIntoView({ block: 'center', behavior: 'smooth' });
    requestAnimationFrame(() => row.scrollIntoView({ block: 'center', behavior: 'auto' }));
    setTimeout(() => row.classList.remove('row-marked'), ROW_MARK_MS);
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

// Force a fresh totals refresh: POST /api/distrakt/refresh bypasses the 24h
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
// current month when its doc already exists (lazy-init only seeds premieres
// once), and to build the month ahead, which is not built by being opened.
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
        // The server's refusals say WHY — the calendar has not reached this
        // month, this past month was never tracked, it is frozen — and a fixed
        // "could not import" threw all of that away, leaving a button that
        // simply did not work. `e.message` carries the server's own sentence
        // when there was one; anything else (a dropped connection, a response
        // that is not JSON) has nothing to say and keeps the generic line.
        const why = e && e.message && e.message !== 'failed' ? e.message : '';
        toast(why || 'Could not import from calendar', false);
        loadMonthData();
    }
}

// Read-only months hide the add/edit affordances and show a banner (abandon
// buttons are also omitted per-row, see showRow). `kind` picks the message:
// 'frozen' = a closed snapshot, 'untracked' = a never-tracked past month.
function applyReadonlyState(readonly, kind) {
    const toolbar = document.querySelector('.distrakt-actions');
    if (toolbar) toolbar.style.visibility = readonly ? 'hidden' : '';
    let note = document.getElementById('distraktFrozenNote');
    if (readonly) {
        // Both states are read-only to the bucketing, but neither is a dead end
        // any more: a finished show can be recorded by hand, and Backup →
        // Backfill fills whole months in from watch history.
        const text = kind === 'untracked'
            ? '🕗 Past month — never tracked. Nothing is bucketed here, but you can record a finished show with ➕ Add show, or fill months in from watch history under Backup.'
            : '🔒 Past month — frozen snapshot. ➕ Add show records something as finished during it.';
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

// The server attaches a `notice` when it fell back to last-known totals (Trakt
// rate-limited or unreachable during a refresh); it is present ONLY on that
// degraded payload, so its presence — not the rate_limited flag, which is just
// metadata on the cause — is what drives the banner. Surface it persistently
// above the list so the shown numbers aren't mistaken for a fresh, correct read.
// A source that could not be read gets the same banner, and it has to: with two
// accounts linked, a season showing one number would otherwise look like the two
// agreeing rather than like only one of them having answered. Never a hard
// failure — whatever DID answer is still on the page.
function renderNotice(d) {
    const el = document.getElementById('distraktNotice');
    if (!el) return;
    // THE SENTENCES ARE THE SERVER'S, FINISHED. There is more than one reason a
    // count can be missing — a service that could not be read, one this instance
    // holds no credential for, a title nothing can look up — and they call for
    // different things from the reader. Building them here meant the wording sat
    // where it could not be checked against the rule that chooses between them
    // (app/distrakt/live.py's unavailable_notices), which is how "could not be
    // reached" came to be shown for a service that was simply absent.
    const lines = [];
    if (d && d.notice) lines.push(d.notice);
    for (const notice of (d && d.source_notices) || []) lines.push(notice);
    if (lines.length) {
        el.textContent = '⚠ ' + lines.join(' ');
        el.hidden = false;
    } else {
        el.textContent = '';
        el.hidden = true;
    }
}
