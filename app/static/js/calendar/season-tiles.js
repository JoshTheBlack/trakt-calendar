// ---- Season info tile enrichment ----
// Lazily fetch each show's current-season summary as its card scrolls into view,
// so the initial page render stays fast. Results are cached server-side.
//
// IT SENDS EVERY ID THE CARD CARRIES, not a Trakt one, and the SERVER builds the
// address it sends them to (`details_url` in app/templating.py) — the same
// builder the details panel's own URL comes from, because "which ids name this
// title" is one question and two answers to it would drift. This used to read
// `card.dataset.traktId` and give up when it was empty, so the line appeared only
// on titles that happened to be linked on Trakt and never on a Simkl-only card.
// Simkl answers this as well as Trakt does; nothing asked.

async function enrichSeasonInfo(card) {
    const el = card.querySelector('[data-role="season-info"]');
    if (!el || el.dataset.loaded) return;
    el.dataset.loaded = '1';
    const media = card.dataset.media;
    const season = card.dataset.season;
    if (media === 'movie' || season === '') return;
    try {
        const res = await fetch(card.dataset.tileUrl);
        const d = await res.json();
        if (!d.ok) return;
        const parts = [];
        if (d.episode_count) parts.push(`<span class="si">📋 <b>${d.episode_count}</b> ep${d.episode_count === 1 ? '' : 's'} · S${season}</span>`);
        if (d.last_aired) parts.push(`<span class="si">🏁 Latest: <b>${esc(d.last_aired)}</b></span>`);
        if (d.next_aired) parts.push(`<span class="si next">📡 Next: <b>${esc(d.next_aired)}</b></span>`);
        if (parts.length) {
            el.innerHTML = parts.join('');
            el.hidden = false;
        }
    } catch (e) { /* non-fatal */ }
}

// One observer per view: a boosted nav brings a whole new set of cards, so the
// previous month's observer is disconnected before a fresh one watches the new
// cards (otherwise it lingers, holding detached nodes).
let seasonObserver = null;
function initSeasonInfo() {
    const cards = document.querySelectorAll('.card[data-season]:not([data-season=""])');
    if (!('IntersectionObserver' in window)) {
        cards.forEach(enrichSeasonInfo);
        return;
    }
    if (seasonObserver) seasonObserver.disconnect();
    seasonObserver = new IntersectionObserver((entries, obs) => {
        entries.forEach(entry => {
            if (entry.isIntersecting) { enrichSeasonInfo(entry.target); obs.unobserve(entry.target); }
        });
    }, { rootMargin: '200px' });
    cards.forEach(c => seasonObserver.observe(c));
}
