// How the month is arranged on screen, and the view preferences that change it:
// card style, day packing, hiding not-watching, and the viewer's timezone.
//
// Body classes (card style, packing, hide-not-watching) ride the persistent
// <body> element rather than the swapped region: they are per-account view prefs
// that do not change across a month or endpoint nav, so a boosted swap must
// leave them alone. The per-PAGE context is read separately — see
// readPageContext in calendar/view.js.

// NOTHING HERE SETS A LAYOUT OR SAVES ONE ANY MORE, and both halves went for
// their own reason. The CLASS is `data-sets-body-class` on the control itself
// (static/js/ui.js), because three controls were doing the same thing three
// ways; the SAVE is an `hx-post` with `hx-json-enc` on the form each control sits
// in (templates/index.html), because a payload that is a form's own fields is
// what that encoder is for and a hand-written fetch was the last reason this
// page had one.
//
// NOTHING HERE RE-PACKS A DAY EITHER. How many columns a day's grid takes, and
// whether hide mode empties it entirely, are the stylesheet's questions about
// that day's own cards (style.css's `--cols` and the collapse rule beside it).
// This file used to answer both by walking every day block after every change —
// a class flip, a mark, a late day — and the caps it worked from were a third
// spelling of numbers that also sat in the stylesheet and in the calendar's
// routes.

// Poster-only wall: if a card is too close to the right edge, open its hover panel
// to the LEFT so it never runs off-screen (and so it can't flicker-wrap).
//
// WHERE A PANEL OPENS DEPENDS ON WHERE THE CARD WAS LAID OUT, which no stylesheet
// rule can compare against the window's edge — hence script. The calendar and a
// shared calendar both draw poster-only cards (`body.card-poster`), so this acts
// on both; on any other page no card wears that style and it does nothing.
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

// ---- The viewer's own timezone ----
// SAVING IT AND RELOADING ARE BOTH MARKUP: the select posts itself with
// `hx-json-enc` and the route answers `HX-Refresh: true`, which htmx turns into
// a reload (read in the vendored source). Day headers and air times are computed
// server-side for the saved zone, so the page genuinely has to come again.
//
// READING THE DEVICE'S OWN ZONE IS THE ONE THING ONLY THIS SIDE KNOWS, and it is
// all that is left here. No automatic detection: the saved default is a
// deliberate choice, and "use my device timezone" is one press away rather than
// something silently applied on load. The press fills the select and lets the
// select's own request go — rather than posting separately — so there is one
// statement of where a timezone is saved and what happens afterwards.
function useDeviceTimezone() {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const select = document.getElementById('tzSelect');
    if (!tz || !select || !select.querySelector(`option[value="${CSS.escape(tz)}"]`)) return;
    select.value = tz;
    select.dispatchEvent(new Event('change', { bubbles: true }));
}

// The 📍 button carries `data-device-timezone` rather than an onclick attribute,
// because the app's Content-Security-Policy runs no script written in markup
// (app/csp.py). Delegated from the document so the settings panel can be
// redrawn under it.
document.addEventListener('click', (e) => {
    if (e.target instanceof Element && e.target.closest('[data-device-timezone]')) useDeviceTimezone();
});

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

// NOTHING HERE GREYS A CHIP ANY MORE, AND THAT IS THE POINT. A day whose every
// title is marked draws nothing with hiding on, so its chip has to stop offering
// to scroll there — and that used to be worked out in the browser, by asking the
// stylesheet whether each day block had collapsed, because both things that
// could change the answer happened here: a mark, and the hide switch.
// NEITHER DOES NOW. A mark is a button posting itself, answered with the cards
// and the affected days' chips redrawn (_marked_cards.html); the hide switch is
// a form posting itself, answered with this month's chips redrawn
// (_view_answer.html). `_day_chip` decides it in both cases, which is the same
// function the page's own chips were drawn by — so there is one rule for what
// makes a chip unreachable rather than a server rule and a browser rule that
// have to agree.
