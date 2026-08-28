// ---- Filtering from a card's own badges ----
// Pressing a certification, country, genre or network on a card offers to add it
// to THIS VIEWER's filters, in or out. Those four are exactly the dimensions the
// per-viewer filter has (app/calendar/filter.py); a card also draws a language
// and a weekday, and neither is something this app filters on.
//
// ALMOST NONE OF THE FEATURE IS IN THIS FILE, WHICH IS THE POINT.
//   The badge is a <details>, so opening, closing, the keyboard and the ARIA are
//   the browser's.
//   The choice is a radio, so the browser holds it and the stylesheet reads it
//   (`:has(:checked)`) — no state is kept here for anything to fall out of step
//   with.
//   The MERGE is the server's. What a spec is — the leading '-', which dimensions
//   fold case, that networks are a list — is app/calendar/filter.py's, and
//   restating any of it here would be that format written twice in two languages.
//
// So one press sends one fact: which dimension, which token, and which way.
//
// A CHANGE LANDS ON THE NEXT READ, not on this one. Filtering is applied while a
// month is assembled server-side, so the calendar in front of you is already
// built — the toast says so rather than leaving somebody to wonder why the card
// they just filtered is still there.

async function saveBadgeFilter(badge, mode) {
    const dimension = badge.dataset.filter;
    const token = badge.dataset.token;
    if (!dimension || !token) return;
    const media = badge.closest('[data-media]')?.dataset.media || '';
    try {
        const res = await fetch('/api/me/filters/badge', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ dimension, token, mode, media })
        });
        const d = await res.json().catch(() => ({}));
        if (!res.ok || !d.ok) {
            toast(d.error || 'Could not save that filter', false);
            return false;
        }
        const label = badge.querySelector('summary')?.textContent.trim() || token;
        toast(mode === 'exclude' ? `Filtering out ${label} — applies on next load`
            : mode === 'include' ? `Showing only ${label} — applies on next load`
            : `${label} filter removed — applies on next load`, true);
        return true;
    } catch (e) {
        console.error(e);
        toast('Could not save that filter', false);
        return false;
    }
}

// THE ONE LISTENER THE BROWSER CANNOT REPLACE, and it exists only because the card
// carries an inline onclick that opens the details modal. An inline handler on an
// ancestor runs while the event BUBBLES, so it fires long before anything bound at
// the document — which is why this captures: pressing a badge must not also open
// the modal behind it. It goes when the inline handlers do.
document.addEventListener('click', (event) => {
    if (event.target.closest('.filter-badge')) event.stopPropagation();
}, true);

// One offer open at a time. `toggle` does not bubble, so it is captured too.
document.addEventListener('toggle', (event) => {
    const badge = event.target.closest?.('.filter-badge');
    if (!badge || !badge.open) return;
    document.querySelectorAll('.filter-badge[open]').forEach(other => {
        if (other !== badge) other.removeAttribute('open');
    });
}, true);

// Choosing is what saves. The radio has already recorded the choice by the time
// this runs, so the badge is coloured whether the request is quick or slow — and
// if it fails, the radio is put back so the card is not left claiming a filter
// nobody stored.
document.addEventListener('change', (event) => {
    const badge = event.target.closest('.filter-badge');
    if (!badge) return;
    const chosen = event.target;
    const previous = badge.dataset.saved || '';
    badge.removeAttribute('open');
    saveBadgeFilter(badge, chosen.value).then(ok => {
        if (ok) {
            badge.dataset.saved = chosen.value;
            return;
        }
        const back = badge.querySelector(`input[value="${previous}"]`);
        if (back) back.checked = true;
    });
});
