// How the month is arranged on screen, and the view preferences that change it:
// card style, day packing, hiding not-watching, and the viewer's timezone.

// Body classes (card style, packing, hide-not-watching) ride the persistent
// <body> element rather than the swapped region: they are per-account view prefs
// that do not change across a month or endpoint nav, so a boosted swap must
// leave them alone. The per-PAGE context is read separately — see
// readPageContext in calendar/view.js.
const BODY = document.body;

// ---- Layout controls: card style + day packing ----
// Applied instantly via <body> classes (pure CSS), then persisted to settings.
//
// NOTHING HERE RE-PACKS A DAY. How many columns a day's grid takes, and whether
// hide mode empties it entirely, are the stylesheet's questions about that day's
// own cards (style.css's `--cols` and the collapse rule beside it). This file
// used to answer both by walking every day block after every change — a class
// flip, a mark, a late day — and the caps it worked from were a third spelling
// of numbers that also sat in the stylesheet and in the calendar's routes.
async function setLayout(key, value) {
    if (key === 'card_style') {
        document.body.classList.remove('card-vertical', 'card-horizontal', 'card-poster');
        document.body.classList.add('card-' + value);
    } else if (key === 'day_packing') {
        document.body.classList.remove('pack-stacked', 'pack-packed');
        document.body.classList.add('pack-' + value);
    }
    // The layout already changed on screen; this only persists it — to this
    // account's own view preferences, so it sticks on the next visit for
    // whoever is signed in, not just an administrator.
    try {
        await fetch('/api/me/prefs', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ [key]: value })
        });
    } catch (e) { console.error(e); }
}

// Poster-only wall: if a card is too close to the right edge, open its hover panel
// to the LEFT so it never runs off-screen (and so it can't flicker-wrap).
document.addEventListener('mouseover', (e) => {
    const card = e.target.closest && e.target.closest('.card');
    if (!card) return;
    if (document.body.classList.contains('card-poster')) {
        const panel = parseInt(getComputedStyle(document.body).getPropertyValue('--panel-w')) || 300;
        const r = card.getBoundingClientRect();
        card.classList.toggle('flip-left', r.right + panel + 24 > window.innerWidth);
    } else if (card.classList.contains('flip-left')) {
        card.classList.remove('flip-left');
    }
});

// ---- Hide / show not-watching ----
async function toggleHideNotWatching() {
    const hide = !BODY.classList.contains('hide-not-watching');
    BODY.classList.toggle('hide-not-watching', hide);
    const label = document.getElementById('hideToggleLabel');
    const btn = document.getElementById('hideToggle');
    label.textContent = hide ? '🚫 Hiding' : '👁️ Showing all';
    btn.classList.toggle('active', hide);
    syncChipReach();
    // Persists to this account's own view preferences (same as setLayout).
    try {
        await fetch('/api/me/prefs', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ hide_not_watching: hide })
        });
    } catch (e) { console.error(e); }
}

// ---- Timezone picker (day/time grouping) ----
// No automatic browser detection: the saved default is a deliberate choice, and
// "use my device timezone" is one click away rather than something silently
// applied on load. Changing it reloads the page, since day headers and air
// times are computed server-side for the viewer's saved zone.
async function setViewerTimezone(tz) {
    if (!tz) return;
    try {
        const res = await fetch('/api/me/timezone', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ timezone: tz })
        });
        if (!res.ok) throw new Error('save failed');
        window.location.reload();
    } catch (e) {
        console.error(e);
        toast('Could not save timezone', false);
    }
}

function useDeviceTimezone() {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (!tz) return;
    const select = document.getElementById('tzSelect');
    if (select) select.value = tz;
    setViewerTimezone(tz);
}

// ---- Jump-to strip: pinned under the header, tucked behind it once scrolled ----
// The strip sticks below the sticky header, so it needs the header's real height
// — it wraps to a second row when narrow — published as --header-h for the CSS to
// pin against. It also needs to know when it WOULD have pinned: from that point
// on it is tucked up behind the header, and the hover band (a CSS ::before on the
// strip) is what brings it back. Listeners are bound once, so a boosted nav can't
// stack them; the measuring re-runs on every init because a nav lands a fresh
// header and a fresh strip.
let dayChipsBound = false;
function syncDayChips() {
    const header = document.querySelector('header.hero');
    const strip = document.querySelector('.day-chips');
    if (!header || !strip) return;
    const headerH = header.offsetHeight;
    document.body.style.setProperty('--header-h', headerH + 'px');
    // Where the strip's slot in the document currently sits on screen. Measured
    // from the element AFTER it, never from the strip itself: a stuck sticky
    // element's own offsetTop/rect track the stuck position, so comparing it
    // against the scroll offset gives a difference that never changes and the
    // tuck would never fire. The neighbour is ordinary in-flow content, and the
    // strip keeps its slot whether stuck or tucked (sticky and transforms both
    // leave layout alone), so this reads the same either way.
    const after = strip.nextElementSibling;
    const naturalTop = after
        ? after.getBoundingClientRect().top - strip.offsetHeight
        : strip.offsetTop - window.scrollY;
    // A pixel of slack: at rest the strip sits exactly on the header's edge, and
    // sub-pixel layout would otherwise flap the class on and off.
    document.body.classList.toggle('chips-tucked', naturalTop < headerH - 1);
}

function initDayChips() {
    if (!dayChipsBound) {
        dayChipsBound = true;
        window.addEventListener('scroll', syncDayChips, { passive: true });
        window.addEventListener('resize', syncDayChips, { passive: true });
    }
    syncDayChips();
}

// A COLLAPSED DAY IS NOT SOMEWHERE TO JUMP TO, so its chip stops looking and
// acting like a destination.
//
// THIS IS WHAT COULD NOT BECOME A STYLESHEET RULE, and the reason is where the
// chip sits: the strip is pinned above <main>, so the day it names is neither an
// ancestor nor a following sibling of it and no selector can reach from one to
// the other. Expressing it as `:has()` on <body> would mean a rule per date —
// thirty-one whole-document selectors re-evaluated every time a card is marked on
// a page that can hold a thousand of them.
//
// IT ASKS THE STYLESHEET RATHER THAN RESTATING IT. Whether a day is collapsed is
// decided in exactly one place (style.css's hide-mode rule), and this reads that
// decision back off the element instead of working the condition out a second
// time — so a change to what "empty" means cannot leave the chips disagreeing
// with the days.
//
// Only days actually in the DOM are touched: a chip for a day whose block has not
// loaded yet is still a perfectly good target, and greying it would be a lie.
function syncChipReach() {
    document.querySelectorAll('.day-block[data-date]').forEach(block => {
        const chip = document.getElementById('day-chip-' + block.dataset.date);
        if (chip) {
            chip.classList.toggle('unreachable',
                getComputedStyle(block).display === 'none');
        }
    });
}
