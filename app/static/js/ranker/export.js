// The export modal: what to render, how big it comes out, and getting the file.
//
// The preview inside it is ranker/export-preview.js and the header-image picker
// is ranker/export-image.js; both are separate because each has its own
// endpoint and its own reasons to change.

// ---------------------------------------------------------------------------
// the export modal
// ---------------------------------------------------------------------------

function openExportModal() {
    const modal = document.getElementById('exportModal');
    const limits = state.limits;
    const cols = document.getElementById('exCols');
    if (!cols.options.length) {
        limits.columns.forEach(n => cols.add(new Option(n + ' columns', n)));
        cols.value = limits.columns.includes(5) ? 5 : limits.columns[0];
        const fmt = document.getElementById('exFmt');
        // Each format's ceiling is stated where it is chosen, so the hard refusal
        // is something the user was already warned about rather than a surprise.
        [['webp', 'WebP — smallest'], ['jpeg', 'JPEG — widest support'],
         ['png', 'PNG — lossless, largest']].forEach(([value, label]) => {
            const option = new Option(label, value);
            option.title = 'Maximum ' + limits.max_dimension[value].toLocaleString() +
                'px in either direction.';
            fmt.add(option);
        });
        document.getElementById('exTitle').value = defaultExportTitle();
        document.getElementById('exUser').value = state.username || '';
    }
    const scope = document.getElementById('exScope');
    scope.innerHTML = '<option value="global">All tiers, consolidated</option>';
    (state.board.categories || []).forEach(cat => {
        scope.add(new Option('Only: ' + (cat.label || 'Untitled'), 'cat:' + cat.uid));
    });
    loadHeaderImageChoices();
    modal.classList.add('open');
    onExportChange();
}

function closeExportModal() {
    document.getElementById('exportModal').classList.remove('open');
    clearTimeout(previewTimer);
}

function defaultExportTitle() {
    const scope = state.board.media_scope;
    const what = scope === 'movie' ? 'Movies' : (scope === 'show' ? 'Shows' : 'Titles');
    return 'Top ' + what + ' of ' + (state.board.year || new Date().getFullYear());
}

function exportOptions() {
    const scope = document.getElementById('exScope').value;
    const header = document.getElementById('exHeader').value;
    return {
        top_x: parseInt(document.getElementById('exTopX').value, 10) || 1,
        columns: parseInt(document.getElementById('exCols').value, 10),
        title: document.getElementById('exTitle').value,
        username: document.getElementById('exUser').value,
        scope: scope.startsWith('cat:') ? 'category' : 'global',
        category_uid: scope.startsWith('cat:') ? scope.slice(4) : null,
        scale: parseFloat(document.getElementById('exScale').value),
        fmt: document.getElementById('exFmt').value,
        show_titles: document.getElementById('exTitles').checked,
        podium: document.getElementById('exPodium').checked,
        // Three shapes, matching what _header_bytes accepts server-side: the
        // bare string for the avatar, and one keyed object each for a saved
        // image and for a connected service's picture. The server validates
        // both names against what this account actually owns — nothing here is
        // trusted to have picked a real one.
        header_image: headerImageSpec(header),
    };
}

function headerImageSpec(header) {
    if (header === 'avatar') return 'avatar';
    if (header.startsWith('img:')) return { image_uid: header.slice(4) };
    if (header.startsWith('provider:')) return { provider: header.slice(9) };
    return null;
}

// How many titles the chosen scope actually contributes — the grid is that many
// or top_x, whichever is smaller.
function scopedCount(options) {
    const cats = state.board.categories || [];
    const contributing = options.scope === 'category'
        ? cats.filter(c => c.uid === options.category_uid)
        : cats.filter(c => !c.is_isolated);
    return contributing.reduce((total, c) => total + c.items.length, 0);
}

// THE SAME ARITHMETIC compute_layout does, from the same constants — which the
// server sends rather than this file restating them, so the number shown here
// and the number the pre-render check refuses on cannot drift apart.
function computeCanvas(count, columns, scale, showTitles, podium) {
    const L = state.limits;
    const s = v => Math.max(1, Math.round(v * scale));
    const tileW = s(L.tile_w), tileH = s(L.tile_h);
    const labelH = s(L.label_h), captionH = showTitles ? s(L.caption_h) : 0;
    const gutter = Math.round(L.gutter * scale), margin = Math.round(L.margin * scale);
    const width = margin * 2 + columns * tileW + (columns - 1) * gutter;
    const contentW = width - margin * 2;
    let y = margin + s(L.header_h);
    let placed = 0;
    const podiumCount = podium ? Math.min(L.podium_ranks, count) : 0;
    if (podiumCount) {
        const podiumW = Math.floor((contentW - (podiumCount - 1) * gutter) / podiumCount);
        y += Math.round(podiumW * tileH / tileW) + labelH + captionH + gutter;
        placed += podiumCount;
    }
    const remaining = count - placed;
    const rows = remaining > 0 ? Math.ceil(remaining / columns) : 0;
    y += rows * (tileH + labelH + captionH + gutter);
    return { width: width, height: (count > 0 ? y - gutter : y) + margin };
}

function onExportChange() {
    showHeaderThumb();
    const options = exportOptions();
    const count = Math.min(options.top_x, scopedCount(options));
    const size = computeCanvas(count, options.columns, options.scale,
                               options.show_titles, options.podium);
    const limit = state.limits.max_dimension[options.fmt];
    const over = size.width > limit || size.height > limit;
    const readout = document.getElementById('exSize');
    readout.classList.toggle('is-over', over);
    readout.textContent = count
        ? (size.width + ' × ' + size.height + 'px' + (over
            ? ' — too tall for ' + options.fmt.toUpperCase() + ' (limit ' +
              limit.toLocaleString() + 'px). Try more columns, half size, or PNG.'
            : ''))
        : 'Nothing tiered to export yet.';
    document.getElementById('exportGo').disabled = over || !count;
    clearTimeout(previewTimer);
    if (!over && count) previewTimer = setTimeout(refreshPreview, 450);
}

async function downloadExport() {
    const button = document.getElementById('exportGo');
    // Disabled while rendering: every render takes one of two instance-wide
    // slots, and a burst of clicks would queue full-size renders behind it.
    button.disabled = true;
    button.textContent = 'Rendering…';
    try {
        const res = await fetch('/api/rankings/boards/' + encodeURIComponent(boardUid()) + '/export', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(exportOptions()),
        });
        if (!res.ok) {
            const data = await res.json().catch(() => ({}));
            toast(data.error || 'Could not export.', false);
            return;
        }
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = filenameFrom(res.headers.get('Content-Disposition')) ||
            ('ranking.' + exportOptions().fmt);
        document.body.appendChild(link);
        link.click();
        link.remove();
        // The blob is megabytes; without this it is held until the page goes.
        URL.revokeObjectURL(url);
        const mb = blob.size / (1024 * 1024);
        toast('Exported — ' + mb.toFixed(1) + ' MB.' +
              (mb > 10 ? ' Over 10 MB: try WebP or half size for Discord.' : ''), true);
    } catch (e) {
        toast('Could not export.', false);
    } finally {
        button.textContent = 'Download';
        // Through onExportChange rather than a plain re-enable, so a size that
        // the format cannot hold stays refused instead of being re-armed by the
        // act of having tried it.
        onExportChange();
    }
}

function filenameFrom(disposition) {
    const match = /filename="([^"]+)"/.exec(disposition || '');
    return match ? match[1] : null;
}

async function copyMarkdown() {
    try {
        const data = await api(
            '/api/rankings/boards/' + encodeURIComponent(boardUid()) + '/export/markdown',
            'POST', exportOptions());
        await navigator.clipboard.writeText(data.markdown);
        toast('Copied ' + data.count + ' titles as Markdown.', true);
    } catch (e) { toast(e.message || 'Could not copy.', false); }
}
