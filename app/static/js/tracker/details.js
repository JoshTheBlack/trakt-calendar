// ---- Show details modal (the calendar's, plus this user's watched episodes) ----

function closeDistraktDetails() {
    document.getElementById('distraktDetailsModal').classList.remove('open');
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
            '<div class="d-empty">⚠️ Could not load details from Trakt.</div>';
    }
}

// The 11-character video id out of any of YouTube's URL shapes. Its own copy
// rather than a shared helper: the calendar's modal has the same three lines, and
// the two have never had a reason to agree beyond both reading YouTube URLs.
function distraktYouTubeId(url) {
    const m = String(url).match(/(?:youtube\.com\/(?:watch\?(?:.*&)?v=|embed\/|v\/)|youtu\.be\/)([\w-]{11})/);
    return m ? m[1] : null;
}

// https://app.trakt.tv/shows/<slug>?season=N&view=episode&episode=M
function traktEpisodeUrl(slug, season, number) {
    if (!slug) return null;
    return `https://app.trakt.tv/shows/${encodeURIComponent(slug)}`
        + `?season=${encodeURIComponent(season)}&view=episode&episode=${encodeURIComponent(number)}`;
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
        const yt = distraktYouTubeId(d.trailer);
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

    const watched = new Set((d.watched_episodes || []).map(Number));
    html += `<div class="details-section-title">📺 Season ${esc(d.season)} Episodes`
        + (watched.size ? ` <span class="ep-watched-count">${watched.size} watched</span>` : '')
        + `</div>`;
    if (d.episodes && d.episodes.length) {
        html += `<div class="ep-list">` + d.episodes.map(ep => {
            const seen = watched.has(Number(ep.number));
            const url = traktEpisodeUrl(d.slug, d.season, ep.number);
            // Every episode gets a marker: a solid tick when this user has watched
            // it, a hollow one when they haven't. Both open that episode on Trakt,
            // so the unwatched ones are the useful link — that is where you go to
            // mark it off.
            const glyph = seen ? '✓' : '○';
            const label = seen ? 'Watched — open on Trakt' : 'Not watched — open on Trakt';
            const cls = 'ep-check' + (seen ? '' : ' unseen');
            const mark = url
                ? `<a class="${cls}" href="${esc(url)}" target="_blank" rel="noopener"
                      title="${label}">${glyph}</a>`
                : `<span class="${cls}" title="${label}">${glyph}</span>`;
            return `
                <div class="ep-row${seen ? ' watched' : ''}">
                    ${mark}
                    <span class="ep-num">E${String(ep.number).padStart(2, '0')}</span>
                    <span class="ep-title">${esc(ep.title)}</span>
                    ${ep.rating ? `<span class="ep-rating">⭐ ${esc(ep.rating)}</span>` : ''}
                    <span class="ep-date">${esc(ep.air_display || 'TBA')}</span>
                </div>`;
        }).join('') + `</div>`;
    } else {
        html += `<div class="d-empty">No episode list available for this season yet.</div>`;
    }

    document.getElementById('distraktDetailsBody').innerHTML = html;
}
