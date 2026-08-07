// ---- Details modal ----

// Which card dataset key holds which service's own id. The card draws one per
// service the group carried, and ALL of them are sent: the server decides who to
// ask, because only it can see whose credentials this instance has filled in
// (app/calendar/detail_source.py). Sending one plus a service name would move
// that decision to the side that cannot answer it.
const DETAIL_ID_KEYS = { trakt: 'traktId', simkl: 'simklId' };

// The query string for a details request, from whatever ids a card carries.
// Empty when the card carries none, which is the one case nothing can describe.
function detailsQuery(dataset, media, season) {
    const parts = [`media=${encodeURIComponent(media)}`];
    let ids = 0;
    for (const [source, key] of Object.entries(DETAIL_ID_KEYS)) {
        const value = dataset[key];
        if (!value) continue;
        ids += 1;
        parts.push(`${source}=${encodeURIComponent(value)}`);
    }
    if (!ids) return null;
    if (season) parts.push(`season=${encodeURIComponent(season)}`);
    return parts.join('&');
}

async function openDetails(card, event) {
    if (event) {
        const interactive = event.target.closest('.watch-toggle, .trakt-btn, a, button');
        if (interactive) return;
    }
    const title = card.dataset.title || card.querySelector('.show-title')?.textContent || 'Details';
    const poster = card.dataset.poster || '/static/images/nopostertv.png';
    const media = card.dataset.media;
    const season = card.dataset.season;

    document.getElementById('detailsTitle').textContent = title;
    buildDetailsActions(card, media, title);
    document.getElementById('detailsBody').innerHTML = '<div class="details-loading">⏳ Loading details…</div>';
    document.getElementById('detailsModal').classList.add('open');

    const q = detailsQuery(card.dataset, media, season);
    if (!q) {
        // No service name in the copy any more. A card with no id at all is a
        // listing nothing can be looked up from, and naming one service made a
        // title the OTHER service could describe perfectly read as broken.
        document.getElementById('detailsBody').innerHTML = '<div class="d-empty">Nothing here can describe this item.</div>';
        return;
    }
    try {
        const res = await fetch(`/api/details?${q}`);
        const d = await res.json();
        if (!d.ok) throw new Error(d.error || 'failed');
        renderDetails(d, poster, media, title, season);
    } catch (e) {
        console.error(e);
        document.getElementById('detailsBody').innerHTML = '<div class="d-empty">⚠️ Could not load details for this item.</div>';
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

// `title` comes from the CARD, not from the payload. Which service answered
// decides what is in the payload — Simkl's catalogue record is stored without a
// title, because the listing that put the card on the page already had one — and
// the modal's heading is set from the card for the same reason.
function renderDetails(d, poster, media, title, season) {
    const chips = [];
    if (d.status) chips.push(`<span class="chip">${esc(d.status)}</span>`);
    if (d.network) chips.push(`<span class="chip network">📡 ${esc(d.network)}</span>`);
    if (d.runtime) chips.push(`<span class="chip">⏱️ ${esc(d.runtime)}m</span>`);
    if (d.rating) chips.push(`<span class="chip country">⭐ ${esc(d.rating)}</span>`);
    if (d.certification) chips.push(`<span class="chip cert" data-cert="${esc(d.certification)}">${esc(d.certification)}</span>`);
    (d.genres || []).forEach(g => chips.push(`<span class="chip">${esc(g)}</span>`));

    let html = `
        <div class="details-hero">
            <img src="${esc(poster)}" alt="${esc(title)} poster">
            <div class="d-meta">
                <div class="d-chips">${chips.join('')}</div>
                ${d.overview ? `<div class="details-overview">${esc(d.overview)}</div>` : '<div class="d-empty">No overview available.</div>'}
            </div>
        </div>`;

    // Trailer. Trakt exposes it via extended=full and Simkl carries a bare
    // YouTube id its provider builds into a URL, so by the time it is here the
    // key means the same thing whoever filled it in. Embed inline, else link out.
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

    // THE SEASON THAT WAS ANSWERED, not the one that was asked about. They are
    // the same for a card whose listing stated a season; where it did not, a
    // source whose episode list holds exactly one season answers with that one
    // (Simkl's calendar files omit the season on anime), and the heading has to
    // say which season the rows below it are.
    const answered = (d.season === null || d.season === undefined) ? season : d.season;
    if (media !== 'movie' && answered) {
        html += `<div class="details-section-title">📺 Season ${esc(answered)} Episodes</div>`;
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
