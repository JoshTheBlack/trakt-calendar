/* The public share page's details modal.
 *
 * Same content as the calendar page — overview, trailer, cast, episodes — so the
 * render below is a copy of app.js's. The one difference is where the data comes
 * from: a token-scoped, rate-limited, CACHE-ONLY endpoint at
 * "<this page's path>/details", rather than the session-only /api/details. It
 * never calls Trakt — it serves back only what the owner's own views already
 * cached (see share_routes._details), so a public page still makes zero Trakt
 * calls. Progressive enhancement: with JavaScript off, the card's own Trakt link
 * still reaches the full details.
 */
function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
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
    const id = d.traktId;
    const season = d.season;

    document.getElementById('detailsTitle').textContent = title;
    document.getElementById('detailsBody').innerHTML = '<div class="details-loading">⏳ Loading details…</div>';
    document.getElementById('detailsModal').classList.add('open');

    if (!id) {
        document.getElementById('detailsBody').innerHTML = '<div class="d-empty">No Trakt id available for this item.</div>';
        return;
    }
    try {
        const q = `media=${encodeURIComponent(media)}&id=${encodeURIComponent(id)}`
            + (season ? `&season=${encodeURIComponent(season)}` : '');
        const res = await fetch(`${shareDetailsBase()}?${q}`);
        const dd = await res.json();
        if (!dd.ok) throw new Error(dd.error || 'failed');
        renderShareDetails(dd, poster, media, season);
    } catch (e) {
        console.error(e);
        document.getElementById('detailsBody').innerHTML = '<div class="d-empty">⚠️ Could not load details.</div>';
    }
}

// Extract a YouTube video id from the various URL shapes Trakt returns.
function youTubeId(url) {
    const m = String(url).match(/(?:youtube\.com\/(?:watch\?(?:.*&)?v=|embed\/|v\/)|youtu\.be\/)([\w-]{11})/);
    return m ? m[1] : null;
}

function renderShareDetails(d, poster, media, season) {
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

function closeShareDetails() {
    document.getElementById('detailsModal').classList.remove('open');
}

document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeShareDetails();
});
