// ---- Show details modal (the calendar's, plus this user's watched episodes) ----

// How a service is written when a tick has to say whose it is. TEXT, NOT A LOGO,
// and that follows a rule stated in _source_logo.html rather than a preference:
// a mark is always part of a CONTROL there, and an attribution on a read-only
// episode row is not something a reader can act on. A mark here would look
// exactly like the flip control on a card and do nothing when it was clicked.
const SERVICE_NAMES = { trakt: 'Trakt', simkl: 'Simkl' };

function closeDistraktDetails() {
    closeModal('distraktDetailsModal');
}

async function openDistraktDetails(row, event) {
    // The row carries its own controls; clicking Abandon must not also open this.
    if (event && event.target.closest('button, a')) return;
    const key = row.dataset.key;
    const season = row.dataset.season;
    const title = row.dataset.title || 'Details';

    document.getElementById('distraktDetailsTitle').textContent =
        `${title} · S${String(season).padStart(2, '0')}`;
    document.getElementById('distraktDetailsBody').innerHTML =
        '<div class="details-loading">⏳ Loading details…</div>';
    document.getElementById('distraktDetailsModal').classList.add('open');

    try {
        const res = await fetch(`/api/distrakt/details?key=${encodeURIComponent(key)}`
            + `&season=${encodeURIComponent(season)}`);
        const d = await res.json();
        if (!d.ok) throw new Error(d.error || 'failed');
        renderDistraktDetails(d);
    } catch (e) {
        console.error(e);
        document.getElementById('distraktDetailsBody').innerHTML =
            // NAMES NO SERVICE, because it does not know which one would have
            // answered and there is more than one: the server picks a source
            // from the row's own ids (app/calendar/detail_source.py), so this
            // said "Trakt" over a title Trakt never listed. The calendar's own
            // modal has always worded it this way.
            '<div class="d-empty">⚠️ Could not load details for this item.</div>';
    }
}

// https://app.trakt.tv/shows/<slug|id>?season=N&view=episode&episode=M
//
// THE SLUG IS PREFERRED AND THE ID IS THE FALLBACK, the same order the Simkl
// link below uses and for the same reason: a service resolves its own numeric id
// perfectly well, but doing so costs it a title lookup and a redirect it would
// not have needed. Sending the readable name when we know it is what both
// services ask for.
//
// A ROW WITH NEITHER USED TO GET NO LINK AT ALL. Trakt's slug arrives on history
// events, so a title with no recent play could carry an id for years and still
// render its tick as plain text — a tick you cannot press to go and correct the
// count it disagrees with, which is the entire purpose of the tick. The id alone
// reaches the right page.
function traktEpisodeUrl(slug, traktId, season, number) {
    const name = slug || (traktId != null && traktId !== '' ? String(traktId) : '');
    if (!name) return null;
    return `https://app.trakt.tv/shows/${encodeURIComponent(name)}`
        + `?season=${encodeURIComponent(season)}&view=episode&episode=${encodeURIComponent(number)}`;
}

// Where a tick goes: the service that holds the record, on the title it is a
// record OF. A tick is what you press to go and correct a count, so it has to
// open the service holding the wrong one — every tick opening Trakt, whichever
// service had recorded the episode, is the fault this replaced.
//
// Trakt can be addressed down to the episode; Simkl is opened at the title,
// which is as deep as an id from a roster row reaches. A service the row carries
// no id for gets no link at all and its tick is drawn as plain text rather than
// as a link somewhere unhelpful.
// EACH SERVICE'S OWN SLUG, AND NOTHING ELSE. Trakt and Simkl both call a title's
// readable name `slug` and disagree on what it is — `the-traitors-2023` against
// `the-traitors` — so the single field a row used to carry held whichever service
// wrote last, with nothing recording which.
//
// THE SHARED FIELD IS NOT A SAFE FALLBACK FOR EITHER SERVICE, and reading it as
// one produced live 404s. A Simkl-only title carried `shingeki-no-kyojin-oad` —
// Simkl's name for it — and this handed that to the Trakt link builder, which
// dutifully built a Trakt URL for a title Trakt has never heard of. Requiring the
// service's id first would not have saved it either: a row holding BOTH ids can
// have its shared slug written by either service, so the value is unattributable
// exactly when both links are being drawn.
//
// An empty answer is not a dead end. The id is the fallback the link builders
// use, both services resolve their own, and naming.py fills the namespaced slug
// in from the stored calendar so the prettier form arrives on its own.
function serviceSlug(service, d) {
    if (service === 'trakt') return d.trakt_slug || '';
    if (service === 'simkl') return d.simkl_slug || '';
    return '';
}

function serviceEpisodeUrl(service, d, number) {
    const ids = d.source_ids || {};
    if (service === 'trakt') {
        return traktEpisodeUrl(serviceSlug('trakt', d), ids.trakt, d.season, number);
    }
    if (service === 'simkl' && ids.simkl) {
        // THE ID IS THE STABLE IDENTIFIER AND THE SLUG IS DECORATION — Simkl says
        // so outright, and asks that the slug be included when it is known. So a
        // title we have no Simkl slug for still links, by id alone.
        const slug = serviceSlug('simkl', d);
        const base = `https://simkl.com/tv/${encodeURIComponent(ids.simkl)}`;
        return slug ? `${base}/${encodeURIComponent(slug)}` : base;
    }
    return '';
}

// ONE TICK PER SERVICE THIS ACCOUNT SYNCS — filled where that service recorded
// the episode, hollow where it did not, and each one a link to that service.
//
// WITH ONE SERVICE IT IS EXACTLY WHAT IT ALWAYS WAS: a single tick, no mark, no
// attribution. Naming the only service there is would put a logo on every row of
// a checklist that has nothing to disambiguate — the same rule the note above
// this list follows, and the one the calendar's cards follow for a field only one
// service filled in.
//
// WITH TWO IT CARRIES EACH SERVICE'S MARK, because the tick is now a control that
// goes somewhere specific and the reader has to know where before pressing it.
// That is the test _source_logo.html sets for drawing a mark at all, and it is
// why the note beside this one is still text: a caption is not something you can
// act on, and this is.
function episodeChecks(d, ep, bySource) {
    const number = Number(ep.number);
    const services = (d.services && d.services.length)
        ? d.services
        : Object.keys(bySource).sort();
    const marks = window.SOURCE_MARKS || {};
    const single = services.length < 2;
    return `<span class="ep-checks">` + (services.length ? services : ['trakt']).map(service => {
        const name = SERVICE_NAMES[service] || service;
        const seen = (bySource[service] || []).map(Number).includes(number);
        const url = serviceEpisodeUrl(service, d, ep.number);
        const glyph = seen ? '✓' : '○';
        const body = single ? glyph : `${marks[service] || ''}${glyph}`;
        const label = `${name} ${seen ? 'recorded this' : 'has not recorded this'}`
            + ` — open on ${name}`;
        const cls = `ep-check svc-${service}` + (seen ? '' : ' unseen');
        return url
            ? `<a class="${cls}" href="${esc(url)}" target="_blank" rel="noopener"
                  title="${esc(label)}">${body}</a>`
            : `<span class="${cls}" title="${esc(label)}">${body}</span>`;
    }).join('') + `</span>`;
}

function renderDistraktDetails(d) {
    const chips = [];
    if (d.status) chips.push(`<span class="chip">${esc(d.status)}</span>`);
    if (d.network) chips.push(`<span class="chip network">📡 ${esc(d.network)}</span>`);
    if (d.runtime) chips.push(`<span class="chip">⏱️ ${esc(d.runtime)}m</span>`);
    if (d.rating) chips.push(`<span class="chip country">⭐ ${esc(d.rating)}</span>`);
    if (d.certification) chips.push(`<span class="chip cert" data-cert="${esc(d.certification)}">${esc(d.certification)}</span>`);
    (d.genres || []).forEach(g => chips.push(`<span class="chip">${esc(g)}</span>`));

    // No poster here: the tracker identifies shows by network logo, and this
    // payload carries no image. Rendering the "no poster" placeholder would just
    // be a large grey rectangle, so the hero is text-only.
    let html = `
        <div class="details-hero no-poster">
            <div class="d-meta">
                <div class="d-chips">${chips.join('')}</div>
                ${d.overview ? `<div class="details-overview">${esc(d.overview)}</div>`
                             : '<div class="d-empty">No overview available.</div>'}
            </div>
        </div>`;

    // Trailer, same as the calendar's modal: embedded when it is a YouTube link
    // (which Trakt's are), otherwise a link out.
    if (d.trailer) {
        const yt = youTubeId(d.trailer);
        html += `<div class="details-section-title">▶️ Trailer</div>`;
        html += yt
            ? `<div class="trailer-embed"><iframe src="https://www.youtube-nocookie.com/embed/${esc(yt)}"
                   title="Trailer" loading="lazy"
                   referrerpolicy="strict-origin-when-cross-origin"
                   allow="accelerometer; encrypted-media; gyroscope; picture-in-picture"
                   allowfullscreen></iframe></div>`
            : `<a class="pill-btn" href="${esc(d.trailer)}" target="_blank" rel="noopener">Watch trailer ↗</a>`;
    }

    if (d.cast && d.cast.length) {
        html += `<div class="details-section-title">🎭 Cast</div><div class="cast-grid">` +
            d.cast.map(c => `
                <div class="cast-member">
                    ${c.headshot ? `<img class="headshot" src="${esc(c.headshot)}" alt="${esc(c.name)}" loading="lazy">`
                                 : `<div class="headshot placeholder">👤</div>`}
                    <div class="c-name">${esc(c.name)}</div>
                    ${c.character ? `<div class="c-char">${esc(c.character)}</div>` : ''}
                </div>`).join('') + `</div>`;
    }

    // A TICK IS THE UNION OF EVERY SERVICE THAT RECORDED THE EPISODE, and the
    // services that recorded it are named wherever they disagree. Watching
    // happens once, so an episode either service saw is one this person watched;
    // but the two records really do differ (one service scrobbles, the other was
    // linked last week), and a tick that showed one service's answer without
    // saying so is what this replaced — see api_distrakt_details for the read.
    const bySource = d.watched_by_source || {};
    const services = Object.keys(bySource).sort();
    const seenBy = new Map();
    for (const service of services) {
        for (const number of (bySource[service] || [])) {
            const key = Number(number);
            if (!seenBy.has(key)) seenBy.set(key, []);
            seenBy.get(key).push(SERVICE_NAMES[service] || service);
        }
    }
    const watched = new Set((d.watched_episodes || []).map(Number));
    // Only where the services disagree, which is the same rule a calendar card
    // follows for a field two services filled in differently: agreement needs no
    // caption, and captioning it everywhere would put a service's name on every
    // row of a one-service account's list.
    const disagree = services.length > 1
        && services.some(service => (bySource[service] || []).length !== watched.size);

    html += `<div class="details-section-title">📺 Season ${esc(d.season)} Episodes`
        + (watched.size ? ` <span class="ep-watched-count">${watched.size} watched</span>` : '')
        + `</div>`;
    if (disagree) {
        html += `<div class="ep-source-note">`
            + services.map(service =>
                `${esc(SERVICE_NAMES[service] || service)} recorded ${(bySource[service] || []).length}`
              ).join(' · ')
            + `</div>`;
    }
    if (d.episodes && d.episodes.length) {
        html += `<div class="ep-list">` + d.episodes.map(ep => {
            const number = Number(ep.number);
            const seen = watched.has(number);
            const recorders = seenBy.get(number) || [];
            return `
                <div class="ep-row${seen ? ' watched' : ''}">
                    ${episodeChecks(d, ep, bySource)}
                    <span class="ep-num">E${String(ep.number).padStart(2, '0')}</span>
                    <span class="ep-title">${esc(ep.title)}</span>
                    ${(disagree && recorders.length) ? `<span class="ep-source">${esc(recorders.join(' · '))}</span>` : ''}
                    ${ep.rating ? `<span class="ep-rating">⭐ ${esc(ep.rating)}</span>` : ''}
                    <span class="ep-date">${esc(ep.air_display || 'TBA')}</span>
                </div>`;
        }).join('') + `</div>`;
    } else {
        html += `<div class="d-empty">No episode list available for this season yet.</div>`;
    }

    document.getElementById('distraktDetailsBody').innerHTML = html;
}
