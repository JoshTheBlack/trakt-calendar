// ---- Backfill months from watch history ----
// Two steps on purpose: the check does all the Trakt work and the server keeps
// the plan, so what you confirm is exactly what gets written — this page never
// hands records back to be stored.
let backfillReady = false;

function setBackfillStatus(text, ok) {
    const el = document.getElementById('backfillStatus');
    el.textContent = text || '';
    el.classList.toggle('distrakt-warn', ok === false);
}

async function loadBackfillRange() {
    try {
        const res = await fetch('/api/distrakt/backfill');
        const d = await res.json();
        if (!d.ok) return;
        document.getElementById('backfillStart').value = d.start;
        document.getElementById('backfillEnd').value = d.end;
    } catch (e) { /* the fields still take a range typed by hand */ }
}

async function checkBackfill() {
    const btn = document.getElementById('backfillCheckBtn');
    const out = document.getElementById('backfillResult');
    backfillReady = false;
    out.hidden = true;
    btn.disabled = true;
    setBackfillStatus('Reading your watch history… this one takes a while.', true);
    try {
        const res = await fetch('/api/distrakt/backfill/survey', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                start: document.getElementById('backfillStart').value,
                end: document.getElementById('backfillEnd').value,
            }),
        });
        const d = await res.json();
        if (!d.ok) throw new Error(d.error || 'failed');
        renderBackfillPlan(d);
    } catch (e) {
        setBackfillStatus(e.message || 'Could not read your watch history.', false);
    } finally {
        btn.disabled = false;
    }
}

function renderBackfillPlan(d) {
    const out = document.getElementById('backfillResult');
    const months = d.months || [];
    if (!months.length) {
        out.hidden = true;
        setBackfillStatus(
            `Nothing to add — looked at ${d.seasons_seen || 0} season(s)`
            + (d.movies_known ? ` and ${d.movies_known} film(s), all already recorded` : '')
            + `, and found nothing in that range that isn't already here.`, true);
        return;
    }
    backfillReady = true;
    // Films get their own line per month rather than one total at the end: they
    // come from the same sweep, and a count you cannot see the contents of is
    // not something to confirm.
    out.innerHTML = months.map(m => {
        const parts = [];
        if (m.count) parts.push(`${m.count} finished`);
        if (m.movie_count) parts.push(`${m.movie_count} film${m.movie_count === 1 ? '' : 's'}`);
        // Said out loud rather than left out: a month whose films are all
        // already recorded looks identical to one where none could be found.
        if (m.movie_known) parts.push(`${m.movie_known} film${m.movie_known === 1 ? '' : 's'} already here`);
        return `
        <details class="distrakt-backfill-month">
            <summary><strong>${esc(m.month)}</strong> — ${parts.join(', ') || 'nothing'}</summary>
            <ul>${m.titles.map(t => `<li>${esc(t)}</li>`).join('')
                + m.movie_titles.map(t => `<li>🎬 ${esc(t)}</li>`).join('')}</ul>
        </details>`;
    }).join('')
        + `<p class="distrakt-note">${months.length} month(s), ${d.total} finished season(s)`
        + (d.movies ? `, and ${d.movies} watched film(s)` : '')
        + `.${(d.skipped || []).length ? ` Already tracked, left alone: ${d.skipped.join(', ')}.` : ''}</p>`
        + `<button type="button" class="btn-ghost small" onclick="applyBackfill()">Write these months</button>`;
    out.hidden = false;
    setBackfillStatus('Nothing has been written yet.', true);
}

async function applyBackfill() {
    if (!backfillReady) return;
    setBackfillStatus('Writing…', true);
    try {
        const res = await fetch('/api/distrakt/backfill/apply', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
        });
        const d = await res.json();
        if (!d.ok) throw new Error(d.error || 'failed');
        backfillReady = false;
        document.getElementById('backfillResult').hidden = true;
        // Only mentions films when it actually wrote some, and names the PAGE
        // rather than quoting a control on it. The label of another screen's
        // field is not a fact this file can keep true — it said to switch "What
        // to import" to Movies, unconditionally, including on runs that found no
        // films at all.
        setBackfillStatus(
            `Wrote ${(d.months || []).length} month(s), ${d.shows} finished season(s)`
            + (d.movies ? `, ${d.movies} film(s)` : '')
            + '.'
            + (d.movies ? ' Films are on your watch record now; bringing them into a board is a separate import from Rankings.' : ''),
            true);
        toast('Months backfilled', true);
        loadBackfillRange();
    } catch (e) {
        setBackfillStatus(e.message || 'Could not write those months.', false);
        toast('Could not backfill', false);
    }
}
