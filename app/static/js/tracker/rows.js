// Rendering the month: the bucket order, the sort inside each bucket, and the
// markup for one show row and one film row.
//
// Building HTML only — what the buttons in a row DO is tracker/actions.js.

const BUCKET_LABELS = {
    cleanup: 'Cleanup', keepup: 'Keepup', new: 'New Shows', returning: 'Returning',
    completed: 'Completed', abandoned: 'Abandoned',
};
const BUCKET_ORDER = ['cleanup', 'keepup', 'new', 'returning', 'completed', 'abandoned'];
const WEEKDAY_ORDER = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

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

// `emptyNote` is the server's own sentence for why this month is empty, when it
// knows one — a month waiting to be imported reads very differently from one
// that simply has nothing on it yet, and the blank list looks identical either
// way. Falls back to the generic line for every month that is just empty.
// A question the history pull raised that only the viewer can settle. Thin on
// purpose: it says only what the history event itself said, because the season
// lookup that would fill in counts and dates is exactly the cost this row exists
// to defer until somebody actually wants it.
//
// Drawn ABOVE every section rather than inside one — it is not a bucket and it
// is not part of the month; it is a question, and a question buried under six
// headings is a question nobody answers.
//
// TWO QUESTIONS SHARE ONE ROW because they read the same way and are answered the
// same way; only the sentence and what ✓ does differ. Saying no is one statement
// either way — do not put this on my list — so both send the same dismissal.
//
// The title and the ids travel on the row's dataset rather than through the
// onclick attribute, for the reason deleteFilm already documents: a show called
// "Good Luck, Have Fun, Don't Die" carries both quote characters, and either one
// ends the attribute early and kills the handler.
const ASK_KINDS = {
    unknown: {
        sentence: 'Should we add this to Distrakt?',
        yesTitle: 'Add this season',
        yes: 'addUnknownSeason',
    },
    givenUp: {
        sentence: "You'd given this up. Add it back to Distrakt?",
        yesTitle: 'Put this season back on your list',
        yes: 'resumeGivenUpSeason',
    },
};

function askRow(u, kind) {
    const ask = ASK_KINDS[kind];
    const ep = `S${String(u.season).padStart(2, '0')}E${String(u.number).padStart(2, '0')}`;
    const label = `${u.title || 'Something'} ${ep}`;
    const args = `'${esc(u.key)}', ${u.season}, this`;
    return `
        <div class="distrakt-unknown-row" data-key="${esc(u.key)}" data-season="${u.season}"
             data-title="${esc(u.title || '')}" data-ids="${esc(JSON.stringify(u.ids || {}))}">
            <span class="distrakt-unknown-text">You watched <strong>${esc(label)}</strong>. ${esc(ask.sentence)}</span>
            <span class="distrakt-row-actions">
                <button type="button" class="btn-ghost small" title="${esc(ask.yesTitle)}"
                        onclick="${ask.yes}(${args})">✓</button>
                <button type="button" class="btn-ghost small" title="Don't ask about this season again"
                        onclick="dismissUnknownSeason(${args})">✗</button>
            </span>
        </div>`;
}

// The ones nothing knows about first: a title the tracker has never heard of is a
// bigger gap than one it holds a verdict on.
function renderAsks(unknown, givenUp) {
    return (unknown || []).map(u => askRow(u, 'unknown')).join('')
        + (givenUp || []).map(u => askRow(u, 'givenUp')).join('');
}

function renderShowList(shows, movies, emptyNote, unknown, givenUp) {
    const host = document.getElementById('distraktShowList');
    const films = movies || [];
    const asks = renderAsks(unknown, givenUp);
    if (!shows.length && !films.length) {
        // The questions still stand on an otherwise empty month: they come from
        // viewing, not from anything the month holds.
        host.innerHTML = asks + `<div class="distrakt-empty">${esc(emptyNote || 'Nothing tracked yet this month.')}</div>`;
        return;
    }
    const groups = {};
    BUCKET_ORDER.forEach(b => groups[b] = []);
    shows.forEach(s => (groups[s.bucket] || (groups[s.bucket] = [])).push(s));

    let html = asks;
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
    // Films watched during the month, in their own block. They were being
    // recorded, counted and imported while appearing nowhere on this page except
    // buried in the POST 2 text, which reads exactly like them not being there.
    if (films.length) {
        html += `<div class="distrakt-bucket-head">Films</div>`;
        html += films.slice().sort(byWatchedAt).map(filmRow).join('');
    }
    host.innerHTML = html;
    setupTitleScroll(host);
}

function byWatchedAt(a, b) {
    return String(a.watched_at || '').localeCompare(String(b.watched_at || ''));
}

// A watched film: no buckets, no counts, no progress — a play on a day. The one
// control it has is ✕, because Trakt's history is not always right about what
// was watched and there is no roster row to correct it through.
function filmRow(m) {
    const day = String(m.watched_at || '').slice(0, 10);
    return `
        <div class="distrakt-show-row distrakt-film-row">
            <span class="distrakt-badge">🎬</span>
            <span class="distrakt-title"><span class="tt">${esc(m.title || 'Untitled')}</span></span>
            <span class="distrakt-season"></span>
            <span class="distrakt-network">Film</span>
            <span class="distrakt-counts">${esc(m.year || '')}</span>
            <span class="distrakt-dates">${esc(day)}</span>
            <span class="distrakt-row-actions">${m.key ? `
                <button type="button" class="btn-ghost small danger" title="Remove this film"
                        data-title="${esc(m.title || '')}"
                        onclick="deleteFilm('${esc(m.key)}', this)">✕</button>` : ''}
            </span>
        </div>`;
}

// A season that was finished and is on the pile again — the episode count moved,
// so there is more of it than there was. Said out loud rather than left to be
// noticed: a title you remember finishing, sitting back on the list with no
// explanation, reads as the page having got it wrong.
// It is a button because dismissing it is the only thing that clears it — not
// time and not the next load — so the mark has to be pressable where it is read.
// The row itself opens the details modal on click; details.js ignores clicks that
// land on a button, so this one does not have to fight it.
function returnMark(s) {
    if (!s.returned) return '';
    return ` <button type="button" class="distrakt-return"
            title="More of this season exists than when you finished it — click when you've seen this"
            onclick="acknowledgeReturn('${esc(s.key)}', ${s.season}, this)">back</button>`;
}

function showRow(s) {
    const isNewRet = s.bucket === 'new' || s.bucket === 'returning';
    // The x/y comes from the server already written out, because when two
    // accounts report different numbers for a season the row shows BOTH, each
    // labelled — and that rule is the same one a closed month is written with, so
    // it has one home rather than a copy here that could disagree with it.
    const xy = s.counts || `${s.watched}/${s.total}`;
    let counts = isNewRet ? `${xy}${s.cadence ? ', ' + s.cadence : ''}`
        : (s.bucket === 'completed') ? '' : xy;
    // New/Returning: premiere (– finale for weekly). Keepup: finale (end date).
    let dates = '';
    if (isNewRet) dates = (s.cadence === 'b') ? (s.premiere || '?/?') : `${s.premiere || '?/?'} – ${s.finale || '?/?'}`;
    else if (s.bucket === 'keepup') dates = s.finale || '?/?';
    // Server couldn't refresh THIS show's totals (rate-limited/unreachable): don't
    // present its last-known numbers as a fresh read — blank them and flag it.
    if (s.unavailable) { counts = ''; dates = 'unavailable — refresh to retry'; }
    // A closed month keeps its ✕ but loses the abandon toggle: what a past month
    // RECORDS can still be corrected (a season you finished years ago and
    // re-watched one episode of does not belong on its list), but its verdicts
    // were settled when it froze and are not up for re-deciding now.
    const remove = `
            <button type="button" class="btn-ghost small danger" onclick="deleteShow('${esc(s.key)}', ${s.season}, event, '${esc(s.added_by || '')}')" title="${monthClosed ? 'Remove from this month' : 'Remove from tracker'}">✕</button>`;
    const actions = monthClosed ? remove : `
            <button type="button" class="btn-ghost small" onclick="toggleAbandon('${esc(s.key)}', ${s.season}, ${!s.abandoned})">${s.abandoned ? 'Un-abandon' : 'Abandon'}</button>` + remove;
    const net = s.network || '';
    // Prefer the TMDB network logo (shared cache with the calendar); if it isn't
    // cached (404) fall back to the mapped emoji token.
    const badge = net
        ? `<img class="distrakt-logo" src="/api/network-logo?name=${encodeURIComponent(net)}&tmdb=${(s.ids || {}).tmdb || ''}" alt="" data-emoji="${esc(emojiFor(net))}" onerror="onLogoError(this)">`
        : esc(emojiFor(net));
    return `
        <div class="distrakt-show-row${s.abandoned ? ' abandoned' : ''}${s.unavailable ? ' unavailable' : ''}" title="${esc(net)}"
             data-key="${esc(s.key)}" data-season="${s.season}" data-title="${esc(s.title)}"
             onclick="openDistraktDetails(this, event)">
            <span class="distrakt-badge">${badge}</span>
            <span class="distrakt-title"><span class="tt">${esc(s.title)}</span>${returnMark(s)}</span>
            <span class="distrakt-season">S${String(s.season).padStart(2, '0')}</span>
            <!-- Spelled out in every bucket, not just as a tooltip: this is the
                 string the emoji map is keyed on, so seeing it is what makes the
                 map editable without guessing. -->
            <span class="distrakt-network">${esc(net || '—')}</span>
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
