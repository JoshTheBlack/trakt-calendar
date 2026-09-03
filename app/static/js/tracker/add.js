// Recording something by hand: a show (search, pick, pick a season) and a film
// (a date and a search).
//
// One file because both are the same bargain — the server is told which title by
// its id map and answers with the recomputed month — and both change together
// when that payload changes.
//
// NEITHER FLOW RENDERS ITS OWN RESULTS LIST. Both search fields carry `hx-get`
// at a fragment route and swap the server's rows straight in
// (templates/_distrakt_search_results.html), so "what a search hit looks like"
// has one home. What is left here is the part a fragment cannot be: deciding
// when a search is worth firing, and what a click on one of those rows means.

// ---- Submitting a search ----
// Enter and the button are the same act, on both modals. An empty box is not a
// query and fires nothing — the check lives here, once, rather than in an
// attribute on each of the two inputs.
function submitAddSearch(inputId) {
    const input = document.getElementById(inputId);
    if (!input.value.trim()) return;
    // A new search invalidates whatever the last pick opened.
    resetShowPick();
    htmx.trigger(input, 'search-submit');
    // The button takes focus when it is what was pressed; handing it back means a
    // query that came back wrong can be corrected without reaching for the mouse.
    input.focus();
}

function onAddSearchKey(event, inputId) {
    if (event.key !== 'Enter') return;
    // These inputs are not in a form, so nothing else would happen anyway — but
    // saying so keeps a later `<form>` from turning Enter into a page load.
    event.preventDefault();
    submitAddSearch(inputId);
}

// ---- Add-show modal: search -> pick show -> pick season -> POST add ----
let pickedShow = null;

function openAddShow() {
    document.getElementById('addSearchInput').value = '';
    document.getElementById('addSearchResults').innerHTML = '';
    resetShowPick();
    // A closed month has nothing live to bucket against, so adding to one means
    // recording something as finished during it. Say which mode this is before
    // anything is picked.
    document.getElementById('addShowTitle').textContent =
        monthClosed ? '➕ Add a finished show' : '➕ Add show';
    document.getElementById('addShowCompletedNote').hidden = !monthClosed;
    document.getElementById('addShowModal').classList.add('open');
    document.getElementById('addSearchInput').focus();
}

function closeAddShow() {
    document.getElementById('addShowModal').classList.remove('open');
}

// Everything one pick put on screen, taken back down: the season panel and the
// refusal note are both answers about a title that is no longer the subject.
function resetShowPick() {
    pickedShow = null;
    document.getElementById('addSeasonPick').hidden = true;
    document.getElementById('addShowUnkeyable').hidden = true;
}

// ONE LISTENER FOR THE WHOLE RESULTS LIST, bound to the container in
// templates/distrakt.html. The rows come from the server and carry what a pick
// needs on themselves, so this works the same whether a row arrived with the
// page or with a fragment — and nothing here has to hold the result list in a
// variable to index back into.
function onShowResultClick(event) {
    const row = event.target.closest('.distrakt-search-row');
    if (row) pickShow(row);
}

// A row says it is a button (role/tabindex), so it has to answer to a keyboard
// like one. Both lists share this: the synthetic click lands on whichever
// container the row is in and takes that list's own path from there.
function onResultKey(event) {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    const row = event.target.closest('.distrakt-search-row[role="button"]');
    if (!row) return;
    event.preventDefault();
    row.click();
}

function onMovieResultClick(event) {
    const row = event.target.closest('.distrakt-search-row');
    // A film with no id the tracker can file it under is rendered refused, with
    // the server's reason already on it; there is nothing for a click to do.
    if (row && !row.hasAttribute('aria-disabled')) addPickedMovie(row);
}

async function pickShow(row) {
    // WHICH SERVICE ANSWERS FOR A ROW BOTH OF THEM RETURNED: the FIRST of the
    // row's [source, id] pairs, which the server writes in registry order. Do
    // not sort these, and do not reach for 'trakt' first — the whole point of
    // the merge is that whoever found a title is who can be asked about it, and
    // a Simkl-only instance has no Trakt entry here at all. They are PAIRS
    // rather than an object precisely so that order is not something a
    // serializer on either side can quietly rearrange.
    const sources = JSON.parse(row.dataset.sourceIds || '[]');
    if (!sources.length) return;
    const [source, sourceId] = sources[0];
    pickedShow = {
        title: row.dataset.title || '',
        network: row.dataset.network || '',
        ids: JSON.parse(row.dataset.ids || '{}'),
    };
    document.getElementById('addShowUnkeyable').hidden = true;
    const panel = document.getElementById('addSeasonPick');
    const list = document.getElementById('addSeasonList');
    document.getElementById('addSeasonShowTitle').textContent = pickedShow.title;
    panel.hidden = false;
    list.innerHTML = '<div class="distrakt-empty">Loading seasons…</div>';
    try {
        const res = await fetch('/api/distrakt/seasons?' + seasonsQuery(source, sourceId));
        const d = await res.json();
        if (!d.ok) {
            list.innerHTML = `<div class="distrakt-empty">${esc(d.error || 'Could not load seasons.')}</div>`;
            toast(d.error || 'Could not load seasons', false);
            return;
        }
        // FOUR ANSWERS FROM THE ONE LOOKUP the season list already pays for: the
        // ids it surfaced (which is what resolves a hit search left bare), the
        // network the row may not have had, the season this title already names
        // for itself, and the list to pick from.
        pickedShow.ids = d.ids || pickedShow.ids;
        // ONLY WHERE THE ROW HAD NONE. The row's network came from whichever
        // source led the merge; this one is the per-title record's, and it fills
        // a gap rather than overriding a source that already answered — a Simkl
        // search hit carries no network, so on a Simkl-only instance this is the
        // only place the roster ever gets one.
        pickedShow.network = pickedShow.network || d.network || '';
        if (d.unkeyable) {
            // Only now — after the lookup that had its chance to fill the gap —
            // is "this cannot be filed" a true thing to say. The server's own
            // sentence, so this and the add route's refusal cannot drift.
            panel.hidden = true;
            const note = document.getElementById('addShowUnkeyable');
            note.textContent = d.unkeyable;
            note.hidden = false;
            return;
        }
        if (d.season !== null && d.season !== undefined) {
            // The hit IS a season — a Simkl anime season-title, which knows which
            // season of the underlying show it is. Asking which season would be
            // asking a question the row already answered, and offering the whole
            // show's list invites picking one nobody searched for.
            panel.hidden = true;
            addPickedShow(d.season);
            return;
        }
        renderSeasons(d.seasons || []);
    } catch (e) {
        list.innerHTML = '<div class="distrakt-empty">Could not load seasons.</div>';
    }
}

// `source` names the service to ask and `id` is THAT service's own id for the
// title — never a shared one. The hit's own ids ride along because the lookup
// answers with only what IT surfaced: Trakt's per-title call adds nothing (its
// search hits are never bare), so without them a perfectly keyable Trakt title
// would come back reading as unfileable.
function seasonsQuery(source, sourceId) {
    const params = new URLSearchParams({
        source, id: sourceId, media: 'show', title: pickedShow.title,
    });
    Object.entries(pickedShow.ids).forEach(([space, value]) => params.set(space, value));
    return params.toString();
}

// The season buttons describe themselves the same way the result rows do, and
// one listener on the list serves all of them — a handler interpolated into each
// button would be one more per-item script for the page's policy to have to
// permit, for no gain over an attribute the button was already going to carry.
function renderSeasons(seasons) {
    const list = document.getElementById('addSeasonList');
    if (!seasons.length) { list.innerHTML = '<div class="distrakt-empty">No aired seasons found.</div>'; return; }
    list.innerHTML = seasons.map(s => `
        <button type="button" class="btn-ghost small" data-season="${esc(s.season)}">
            S${String(s.season).padStart(2, '0')} (${esc(s.episode_count)} eps)
        </button>
    `).join('');
}

function onSeasonClick(event) {
    const button = event.target.closest('button[data-season]');
    if (button) addPickedShow(Number(button.dataset.season));
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
        // NOTHING WAS ADDED YET. The season's own history says it has been
        // finished before, and which record that should become depends on an
        // answer only the viewer has — so the server wrote nothing and asked.
        // Checked before the month is applied, because there is no month here.
        if (d.needs_decision) {
            // THE PICKER GOES AWAY FIRST. Two overlays were open at once and the
            // question drew UNDER the one that raised it, so it could not be
            // reached without dismissing the picker on top of it. Closing is
            // right on its own terms too: the pick is made, and the question is
            // the next step rather than a second thing to look at beside it.
            // `pickedShow` survives this — closing hides the modal and does not
            // reset the pick — which is what lets the answer perform the add.
            closeAddShow();
            openRewatchPrompt(d.needs_decision, season);
            return;
        }
        announceAdded(season, asFinished);
        applyMonthResponse(d);  // mutation returns the recomputed month (1d)
    } catch (e) {
        toast(e.message || 'Could not add show', false);
    }
}

function announceAdded(season, asFinished) {
    const label = `${pickedShow.title} S${String(season).padStart(2, '0')}`;
    toast(asFinished ? `Recorded ${label} as finished` : `Added ${label}`, true);
    closeAddShow();
}

// ---- Re-watching a season you already finished ----
// THE ADD IS NOT DONE YET WHEN THIS OPENS. The server refused to guess and wrote
// nothing, so answering is what performs the add — which is why both buttons
// re-POST the original pick with the answer attached, and why closing this
// leaves the season off the list rather than half on it.

let pendingRewatch = null;

function openRewatchPrompt(prompt, season) {
    pendingRewatch = { ...prompt, season: season };
    const when = prompt.completed_on
        ? new Date(prompt.completed_on + 'T00:00:00').toLocaleDateString(
            undefined, { day: 'numeric', month: 'long', year: 'numeric' })
        : 'some time ago';
    document.getElementById('rewatchQuestion').textContent =
        `Your history says you finished ${prompt.title} season ${prompt.season} on ${when}.`;
    // WHERE A FRESH RUN STARTS, EDITABLE. The offered day is the one after that
    // old finish, which is what makes a new pass start empty; moving it earlier
    // is how somebody who watched an episode or two before adding the season
    // gets those counted.
    document.getElementById('rewatchFrom').value = prompt.suggested_from || '';
    document.getElementById('rewatchModal').classList.add('open');
}

function closeRewatchPrompt() {
    pendingRewatch = null;
    document.getElementById('rewatchModal').classList.remove('open');
}

async function answerRewatch(fresh) {
    if (!pendingRewatch) { return; }
    const asked = pendingRewatch;
    const from = fresh ? (document.getElementById('rewatchFrom').value || asked.suggested_from) : '';
    closeRewatchPrompt();
    try {
        // THE SAME ROUTE AS THE ADD, because this IS the add: one place decides
        // what a season becomes, and it now has the answer it was missing.
        const res = await fetch('/api/distrakt/add', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                year: window.DISTRAKT_YEAR, month: window.DISTRAKT_MONTH,
                ids: pickedShow.ids, title: pickedShow.title,
                network: pickedShow.network, season: asked.season,
                decided: true, history_from: from,
            })
        });
        const d = await res.json();
        if (!d.ok) throw new Error(d.error || 'failed');
        // ONE TOAST AND IT SAYS WHICH ANSWER LANDED. "Added" alone would be
        // true of both and is the part the viewer already knows; what they
        // cannot see from the row is whether their earlier viewing counts.
        toast(fresh ? `Added ${asked.title} S${String(asked.season).padStart(2, '0')} — earlier viewings do not count`
                    : `Added ${asked.title} S${String(asked.season).padStart(2, '0')}, counting what you have watched`,
              true);
        applyMonthResponse(d);
    } catch (e) {
        toast(e.message || 'Could not add show', false);
    }
}

// ---- Add a film ----
// Films have no roster, no buckets and no progress: one is a play on a day. So
// this flow is a date and a search, and the month it lands in follows from the
// date rather than from whichever month happens to be on screen. There is no
// season step here and there never will be, which is also why a film row that
// names no shared id is refused where it is drawn rather than on the click: the
// show flow's click already buys a lookup that could resolve one, and this one
// has nothing to hide a round trip inside.

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
    document.getElementById('addMovieModal').classList.add('open');
    document.getElementById('addMovieSearch').focus();
}

function closeAddMovie() {
    document.getElementById('addMovieModal').classList.remove('open');
}

async function addPickedMovie(row) {
    const title = row.dataset.title || '';
    const year = row.dataset.year ? Number(row.dataset.year) : null;
    try {
        const res = await fetch('/api/distrakt/add-movie', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                ids: JSON.parse(row.dataset.ids || '{}'), title, year,
                watched_on: document.getElementById('addMovieDate').value,
                // Which month to re-render afterwards: the one on screen, which
                // is not necessarily the one the film was filed under.
                year_view: window.DISTRAKT_YEAR, month_view: window.DISTRAKT_MONTH,
            }),
        });
        const d = await res.json();
        if (!d.ok) throw new Error(d.error || 'failed');
        const day = document.getElementById('addMovieDate').value;
        toast(`Recorded ${title} — watched ${day}`, true);
        closeAddMovie();
        applyMonthResponse(d);
    } catch (e) {
        toast(e.message || 'Could not add that film', false);
    }
}
