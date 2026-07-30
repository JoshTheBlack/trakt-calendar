// The two Discord posts this page exists to produce: the copy blocks themselves
// and the share link embedded in POST 1.

function renderCopyBlocks(post1, post2) {
    document.getElementById('post1Text').value = post1;
    document.getElementById('post2Text').value = post2;
}

// ---- The share link embedded in POST 1 ----
// The link itself is rendered into post1 server-side; these two selectors only
// choose WHICH link and which view, then reload the month so the copy block
// reflects the change immediately.
const LINK_KIND_LABELS = { token: 'Private link', username: 'Username link', slug: 'Custom link' };

function renderPostLink(d) {
    const kindSel = document.getElementById('postLinkKind');
    const endpointSel = document.getElementById('postLinkEndpoint');
    if (!kindSel || !endpointSel) return;
    // Only forms that would actually resolve are offered — a selector entry that
    // silently produces no link is worse than not being there.
    const options = [`<option value="">Link: as shared (${LINK_KIND_LABELS[d.preferred_kind] || d.preferred_kind})</option>`];
    Object.keys(LINK_KIND_LABELS).forEach(kind => {
        if (d.available[kind]) options.push(`<option value="${kind}">Link: ${LINK_KIND_LABELS[kind]}</option>`);
    });
    kindSel.innerHTML = options.join('');
    kindSel.value = d.kind || '';
    endpointSel.value = d.endpoint || '';
    // Nothing publishable at all (no public base URL configured, or every form
    // switched off): the post omits the link, so hide the controls that pick it.
    const usable = !d.base_url_missing && Object.values(d.available).some(Boolean);
    kindSel.hidden = !usable;
    endpointSel.hidden = !usable;
}

async function loadPostLink() {
    try {
        const res = await fetch('/api/distrakt/share-link');
        const d = await res.json();
        if (d.ok) renderPostLink(d);
    } catch (e) { /* the copy block still works without the selectors */ }
}

async function savePostLink() {
    const kindSel = document.getElementById('postLinkKind');
    const endpointSel = document.getElementById('postLinkEndpoint');
    try {
        const res = await fetch('/api/distrakt/share-link', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ kind: kindSel.value, endpoint: endpointSel.value }),
        });
        const d = await res.json();
        if (!d.ok) throw new Error(d.error || 'failed');
        renderPostLink(d);
        loadMonthData();
    } catch (e) {
        toast('Could not save the post link', false);
    }
}

async function copyBlock(which) {
    const el = document.getElementById(which === 'post1' ? 'post1Text' : 'post2Text');
    try {
        await navigator.clipboard.writeText(el.value);
        toast('Copied to clipboard', true);
    } catch (e) {
        el.select();
        toast('Could not copy — text selected instead', false);
    }
}
