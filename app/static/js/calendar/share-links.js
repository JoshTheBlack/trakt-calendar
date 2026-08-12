// ---- Public share links (Share panel) ----
let shareState = null;
let shareSlugTimer = null;

function renderShare() {
    if (!shareState) return;
    // public_base_url is an admin-only setting, so an ordinary user can only be
    // annoyed by this, never act on it.
    document.getElementById('share_no_base_url').hidden =
        !(shareState.base_url_missing && window.IS_ADMIN);

    const kind = shareState.preferred_kind;
    document.getElementById('share_kind').value = kind;
    // Each control belongs to exactly one form: the custom name only means
    // anything for the slug link, and rotating only applies to the token.
    document.getElementById('share_slug_field').hidden = kind !== 'slug';
    document.getElementById('share_rotate').hidden = kind !== 'token';
    document.getElementById('share_slug_input').value = shareState.custom_slug || '';

    const url = shareState.urls[kind];
    const box = document.getElementById('share_url');
    box.value = url || '';
    // A slug link has nothing to resolve to until a name is saved; say which of
    // the two reasons the box is empty rather than leaving it blank.
    box.placeholder = shareState.base_url_missing
        ? 'No public base URL set'
        : (kind === 'slug' ? 'Pick a custom name above' : 'No link yet');
    renderShareView();
}

// The Sources ticks, or an empty list on an instance that draws none of them.
function shareSourceTicks() {
    return [...document.querySelectorAll('#share_view_sources input[type=checkbox]')];
}

// Rebuild the Sources ticks for the calendar the LINK opens on, keeping whatever
// was ticked that is still on offer.
//
// WHICH CALENDAR DECIDES, AND IT IS THE PANEL'S RATHER THAN THE PAGE'S. The
// link's Calendar option need not be the one being looked at, so a panel that
// kept the page's answer would go on offering a service the link's own calendar
// cannot use — and would keep the block drawn on Season Finales, which only one
// service publishes and which therefore has nothing to choose between.
//
// A TICK THAT IS NO LONGER OFFERED IS DROPPED, deliberately. Switching a link to
// a calendar a service does not publish means that service can no longer be what
// fills it, and carrying the tick invisibly would leave the link saying
// something the panel had stopped showing.
function renderShareSourceTicks(endpointKey, selected) {
    const field = document.getElementById('share_view_sources_field');
    const box = document.getElementById('share_view_sources');
    if (!field || !box) return;
    const choices = (window.SHARE_SOURCE_CHOICES || {})[endpointKey] || [];
    field.hidden = !choices.length;
    box.innerHTML = choices.map(choice => `<label class="source-tick">`
        + `<input type="checkbox" data-source="${choice.value}"`
        + `${selected.has(choice.value) ? ' checked' : ''} onchange="saveShareView()">`
        + `<span>${choice.label}</span></label>`).join('');
}

// The link's calendar changed: re-ask which services could fill it, then save.
// In that order — saving first would write the ticks from the calendar the owner
// has just moved away from.
function onShareEndpointChange() {
    const ticked = new Set(shareSourceTicks().filter(cb => cb.checked).map(cb => cb.dataset.source));
    renderShareSourceTicks(document.getElementById('share_view_endpoint').value, ticked);
    saveShareView();
}

// The link's display options. A null link_view means the URL goes out bare, so
// whoever opens it sees whatever the owner's calendar currently resolves to;
// otherwise the options below are written into the query string. Neither case
// touches the owner's own view — that is the whole point of storing them here
// rather than reusing the calendar preferences.
function renderShareView() {
    const view = shareState.link_view;
    const custom = !!view;
    document.querySelector('input[name="share_view_mode"][value="current"]').checked = !custom;
    document.querySelector('input[name="share_view_mode"][value="custom"]').checked = custom;
    document.getElementById('share_view_options').hidden = !custom;
    if (!custom) {
        // A hidden panel keeps whatever was last in it, and a stale pinned month
        // would then be written straight back the next time "custom" is chosen.
        setSharePinnedMonth('', null);
        return;
    }
    if (view.endpoint) document.getElementById('share_view_endpoint').value = view.endpoint;
    if (view.tz) document.getElementById('share_view_tz').value = view.tz;
    if (view.card) document.getElementById('share_view_card').value = view.card;
    if (view.packing) document.getElementById('share_view_packing').value = view.packing;
    document.getElementById('share_view_hidenw').checked = view.hidenw === '1';
    // The ticked services, from the set the link names. Absent entirely on an
    // instance where fewer than two services could fill this calendar — the
    // template draws no control there — so every read and write of these is
    // guarded rather than assuming they are on the page.
    //
    // A stored 'auto' ticks nothing, which is correct rather than a gap: it
    // names no services, so under a link that can only narrow it comes out the
    // same as naming none at all. Saving from this panel then writes the ticks,
    // which is what the owner is looking at.
    //
    // REBUILT RATHER THAN JUST RE-TICKED, because the link's own calendar is
    // what decides which ticks exist at all, and this runs after that select has
    // been set from the stored view above.
    renderShareSourceTicks(document.getElementById('share_view_endpoint').value,
                           new Set((view.source || '').split('+')));
    setSharePinnedMonth(view.month || '', view.year || null);
}

// The "opens on" pair. No month means the link carries no year/month at all and
// lands on whatever month it is opened in, so the year select has nothing to say
// and is hidden rather than left offering a year that isn't used.
function setSharePinnedMonth(month, year) {
    const monthSel = document.getElementById('share_view_month');
    const yearSel = document.getElementById('share_view_year');
    const now = new Date().getFullYear();
    const years = [];
    for (let y = now - 1; y <= now + 3; y++) years.push(y);
    // A link pinned years ago still shows its own year rather than snapping to
    // one the owner never picked.
    if (year && !years.includes(Number(year))) years.push(Number(year));
    years.sort((a, b) => a - b);
    yearSel.innerHTML = years.map(y => `<option value="${y}">${y}</option>`).join('');
    yearSel.value = String(year || now);
    monthSel.value = month;
    yearSel.hidden = !month;
}

async function postShareView(view) {
    try {
        const res = await fetch('/api/me/share/view', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ view })
        });
        const d = await res.json();
        if (!d.ok) throw new Error(d.error || 'failed');
        shareState = d;
        renderShare();
    } catch (e) { toast('Could not update the link options', false); }
}

function setShareViewMode(mode) {
    if (mode === 'current') { postShareView(null); return; }
    // Switching to custom seeds the controls from what this page is currently
    // showing, so the first save reproduces the link the owner already had
    // rather than snapping it to some unrelated default.
    document.getElementById('share_view_options').hidden = false;
    saveShareView();
}

function saveShareView() {
    const month = document.getElementById('share_view_month').value;
    // Show the year the moment a month is chosen, without waiting for the round
    // trip that will re-render this panel anyway.
    document.getElementById('share_view_year').hidden = !month;
    const view = {
        endpoint: document.getElementById('share_view_endpoint').value,
        tz: document.getElementById('share_view_tz').value,
        card: document.getElementById('share_view_card').value,
        packing: document.getElementById('share_view_packing').value,
        hidenw: document.getElementById('share_view_hidenw').checked ? '1' : '0',
    };
    // Nothing ticked means "my sources": the link carries no source at all and
    // the page resolves the owner's own preference, so the key is left out
    // rather than written as a blank the server would have to read as absent.
    // Several ticked is spelled the way every other part of the app spells a set
    // of services, joined — see app/sources/prefs.py.
    const ticked = shareSourceTicks().filter(cb => cb.checked).map(cb => cb.dataset.source);
    if (ticked.length) view.source = ticked.join('+');
    // Both or neither — a month pinned without its year would mean a different
    // month once the year turned over, and the server rejects the half of a pair.
    if (month) {
        view.month = month;
        view.year = document.getElementById('share_view_year').value;
    }
    postShareView(view);
}

async function openShare() {
    try {
        const res = await fetch('/api/me/share', { cache: 'no-store' });
        shareState = await res.json();
        renderShare();
    } catch (e) { console.error(e); }
    document.getElementById('shareModal').classList.add('open');
}

function closeShare() {
    document.getElementById('shareModal').classList.remove('open');
}

// One link at a time: this publishes the chosen form and retires the other two,
// so the dropdown is the only sharing switch there is.
async function setShareKind(kind) {
    try {
        const res = await fetch('/api/me/share/active', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ kind })
        });
        shareState = await res.json();
        renderShare();
    } catch (e) { toast('Could not update sharing', false); }
}

function checkShareSlug() {
    clearTimeout(shareSlugTimer);
    const slug = document.getElementById('share_slug_input').value.trim();
    const status = document.getElementById('share_slug_status');
    if (!slug) { status.textContent = ''; status.className = 'hint'; return; }
    status.textContent = 'Checking…';
    status.className = 'hint';
    shareSlugTimer = setTimeout(async () => {
        try {
            const res = await fetch('/api/me/share/slug-check?slug=' + encodeURIComponent(slug), { cache: 'no-store' });
            const d = await res.json();
            if (d.available) {
                status.textContent = 'Available — saving…';
                status.className = 'hint ok';
                const saveRes = await fetch('/api/me/share/slug', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ slug })
                });
                const saved = await saveRes.json();
                if (saved.ok) {
                    shareState = saved;
                    renderShare();
                    status.textContent = 'Saved';
                    status.className = 'hint ok';
                } else {
                    status.textContent = saved.error || 'Could not save';
                    status.className = 'hint err';
                }
            } else {
                status.textContent = d.error || 'Not available';
                status.className = 'hint err';
            }
        } catch (e) {
            status.textContent = 'Could not check availability';
            status.className = 'hint err';
        }
    }, 500);
}

async function rotateShareToken(event) {
    confirmInline(event.currentTarget,
        'Rotate the token link? The old link will stop working immediately.',
        async () => {
            try {
                const res = await fetch('/api/me/share/rotate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
                shareState = await res.json();
                renderShare();
                toast('Token link rotated', true);
            } catch (e) { toast('Could not rotate token', false); }
        }, { danger: true });
}

function copyShareUrl() {
    const input = document.getElementById('share_url');
    if (!input || !input.value) return;
    navigator.clipboard.writeText(input.value).then(
        () => toast('Link copied', true),
        () => toast('Could not copy link', false)
    );
}
