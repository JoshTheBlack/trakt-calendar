// ---------------------------------------------------------------------------
// the optional sources
// ---------------------------------------------------------------------------

function openImportModal() {
    document.getElementById('importModal').classList.add('open');
    refreshImportYears();
}

function closeImportModal() { document.getElementById('importModal').classList.remove('open'); }

// Only the years this account actually has data for — the answer comes from the
// sources endpoint, which is the only thing that knows.
function refreshImportYears() {
    const media = document.getElementById('importMedia').value;
    const select = document.getElementById('importYear');
    const years = ((state.sources.import || {}).years || {})[media] || [];
    select.innerHTML = '<option value="">Any year</option>';
    years.forEach(year => {
        const option = document.createElement('option');
        option.value = year;
        option.textContent = year;
        select.appendChild(option);
    });
}

async function runImport() {
    const button = document.getElementById('importGo');
    button.disabled = true;
    try {
        const data = await api(
            '/api/rankings/boards/' + encodeURIComponent(boardUid()) + '/import/tracker', 'POST',
            { media: document.getElementById('importMedia').value,
              year: document.getElementById('importYear').value || null });
        toast('Found ' + data.found + ', added ' + data.added + '.', true);
        window.location.reload();
    } catch (e) {
        toast(e.message, false);
        button.disabled = false;
    }
}

async function seedFromRatings(commit) {
    try {
        const data = await api(
            '/api/rankings/boards/' + encodeURIComponent(boardUid()) + '/seed/ratings', 'POST',
            { commit: !!commit });
        if (commit) { window.location.reload(); return; }
        // The counts before anything is written — a seed rearranges a board, so
        // it says what it is about to do first.
        const ok = await ask({
            title: 'Seed from ratings',
            input: false,
            confirmText: 'Seed the board',
            message: 'This would add ' + data.titles_added + ' title(s), place ' +
                data.titles_placed + ' and create ' + data.tiers_created + ' tier(s). ' +
                data.already_placed + ' are already in a tier and will not be moved.',
        });
        if (ok) await seedFromRatings(true);
    } catch (e) { toast(e.message, false); }
}
