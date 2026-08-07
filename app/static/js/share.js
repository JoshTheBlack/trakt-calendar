/* The public share page's details modal.
 *
 * Same content as the calendar page — overview, trailer, cast, episodes — so the
 * render below is a copy of the calendar's details modal. The one difference is
 * where the data comes from: a token-scoped, rate-limited, CACHE-ONLY endpoint at
 * "<this page's path>/details", rather than the session-only /api/details. It
 * never calls a source — it serves back only what the owner's own views already
 * cached (see share_routes._details), so a public page still makes zero outbound
 * calls. Progressive enhancement: with JavaScript off, the card's own outbound
 * button still reaches the full details.
 *
 * WHICH SERVICE ANSWERS IS THE SERVER'S DECISION, on both pages, and the card
 * simply hands over every id it carries — see the calendar modal's own
 * DETAIL_ID_KEYS for why. A card only Simkl listed opens on Simkl's answer here
 * exactly as it does on the owner's own calendar.
 */

// One query-string parameter per service's own id, from a card's dataset. The
// same shape the calendar's modal sends, because the endpoint behind both is the
// same code with the fetch switched off.
const SHARE_DETAIL_ID_KEYS = { trakt: 'traktId', simkl: 'simklId' };

function shareDetailsQuery(dataset, media, season) {
    const parts = [`media=${encodeURIComponent(media)}`];
    let ids = 0;
    for (const [source, key] of Object.entries(SHARE_DETAIL_ID_KEYS)) {
        const value = dataset[key];
        if (!value) continue;
        ids += 1;
        parts.push(`${source}=${encodeURIComponent(value)}`);
    }
    if (!ids) return null;
    if (season) parts.push(`season=${encodeURIComponent(season)}`);
    return parts.join('&');
}

// Where the details endpoint lives for whichever share URL this page was reached
// by (/s/<token>, /u/<name>, /c/<slug>): the current path plus "/details".
function shareDetailsBase() {
    return window.location.pathname.replace(/\/+$/, '') + '/details';
}

async function openShareDetails(card, event) {
    if (event) {
        // Let the poster's own Trakt link (and any future control) act normally.
        if (event.target.closest('a, button')) return;
    }
    const d = card.dataset;
    const title = d.title || 'Details';
    const poster = d.poster || '/static/images/nopostertv.png';
    const media = d.media;
    const season = d.season;

    document.getElementById('detailsTitle').textContent = title;
    document.getElementById('detailsBody').innerHTML = '<div class="details-loading">⏳ Loading details…</div>';
    document.getElementById('detailsModal').classList.add('open');

    const q = shareDetailsQuery(d, media, season);
    if (!q) {
        document.getElementById('detailsBody').innerHTML = '<div class="d-empty">Nothing here can describe this item.</div>';
        return;
    }
    try {
        const res = await fetch(`${shareDetailsBase()}?${q}`);
        const dd = await res.json();
        if (!dd.ok) throw new Error(dd.error || 'failed');
        renderShareDetails(dd, poster, media, title, season);
    } catch (e) {
        console.error(e);
        document.getElementById('detailsBody').innerHTML = '<div class="d-empty">⚠️ Could not load details.</div>';
    }
}

// `title` comes from the CARD for the same reason it does on the calendar page:
// which service answered decides what is in the payload, and Simkl's catalogue
// record is stored without a title.
function renderShareDetails(d, poster, media, title, season) {
    const chips = [];
    if (d.status) chips.push(`<span class="chip">${esc(d.status)}</span>`);
    if (d.network) chips.push(`<span class="chip network">📡 ${esc(d.network)}</span>`);
    if (d.runtime) chips.push(`<span class="chip">⏱️ ${esc(d.runtime)}m</span>`);
    if (d.rating) chips.push(`<span class="chip country">⭐ ${esc(d.rating)}</span>`);
    (d.genres || []).forEach(g => chips.push(`<span class="chip">${esc(g)}</span>`));

    let html = `
        <div class="details-hero">
            <img src="${esc(poster)}" alt="${esc(title)} poster">
            <div class="d-meta">
                <div class="d-chips">${chips.join('')}</div>
                ${d.overview ? `<div class="details-overview">${esc(d.overview)}</div>` : '<div class="d-empty">No overview available.</div>'}
            </div>
        </div>`;

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

    // The season that was ANSWERED — see the calendar modal's note; a listing
    // that stated no season can still be answered by a source with one.
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

function closeShareDetails() {
    document.getElementById('detailsModal').classList.remove('open');
}

document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeShareDetails();
});
