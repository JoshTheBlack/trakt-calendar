// ---- Calendar search ----
// TWO REQUESTS THAT COST DIFFERENT THINGS, and the whole of this file is about
// keeping them apart. Typing asks the server what THIS CALENDAR already holds:
// an index read, no outbound call, safe to run while somebody is still typing.
// Pressing Enter or the button additionally asks the registered services, which
// spends real requests — so it happens on a deliberate act and never on a
// keystroke.
//
// The server renders both answers, through the same partial, so this file holds
// no markup: it decides WHEN to ask and swaps in what comes back.

let calendarSearchTimer = null;
let calendarSearchSeq = 0;

// LONG ENOUGH THAT A FAST TYPIST MAKES ONE REQUEST PER WORD, short enough that
// results feel like they are keeping up. The stored half is cheap, so this is
// about not making the server do the same work four times for one word rather
// than about protecting anything.
const CALENDAR_SEARCH_DEBOUNCE_MS = 180;

function openCalendarSearch() {
    document.getElementById('searchModal').classList.add('open');
    const input = document.getElementById('searchInput');
    input.focus();
    input.select();
}

function closeCalendarSearch() {
    document.getElementById('searchModal').classList.remove('open');
}

function onCalendarSearchInput() {
    clearTimeout(calendarSearchTimer);
    calendarSearchTimer = setTimeout(() => runCalendarSearch(false), CALENDAR_SEARCH_DEBOUNCE_MS);
}

// `live` IS THE ONLY THING THAT SPENDS A REQUEST AGAINST A SERVICE. Returns
// false so it can sit on a form's onsubmit without navigating.
async function runCalendarSearch(live) {
    clearTimeout(calendarSearchTimer);
    const query = document.getElementById('searchInput').value.trim();
    const target = document.getElementById('searchResults');
    // EVERY ANSWER CARRIES THE SEQUENCE IT WAS ASKED IN, and a stale one is
    // dropped rather than rendered. Without this, a slow response for "sev"
    // lands after a fast one for "severance" and the reader watches their
    // results go backwards — the more likely the faster the typist.
    const mine = ++calendarSearchSeq;
    if (live) { target.setAttribute('aria-busy', 'true'); }
    try {
        const url = `/calendar/search?q=${encodeURIComponent(query)}${live ? '&live=1' : ''}`;
        const res = await fetch(url, { headers: { 'Accept': 'text/html' } });
        const html = await res.text();
        if (mine !== calendarSearchSeq) { return false; }
        if (!res.ok) {
            toast('Could not search', false);
            return false;
        }
        target.innerHTML = html;
    } catch (e) {
        if (mine === calendarSearchSeq) { toast('Could not reach the server', false); }
    } finally {
        if (mine === calendarSearchSeq) { target.removeAttribute('aria-busy'); }
    }
    return false;
}

// THE CARD A JUMP WAS AIMED AT, once the month it lives on has painted.
//
// IT WAITS FOR THE DAY RATHER THAN ASSUMING IT. A calendar ships its first few
// day blocks inline and fetches the rest when they are scrolled to, so a jump
// into a later month lands on a placeholder that has not become cards yet. The
// observer is what makes the highlight survive that; without it the card is
// found on some arrivals and not others, depending entirely on how far down the
// month the target happened to be.
function highlightJumpTarget() {
    const wanted = new URLSearchParams(window.location.search).get('highlight');
    if (!wanted) { return; }

    const settle = (card) => {
        card.classList.add('jump-target');
        card.scrollIntoView({ block: 'center', behavior: 'smooth' });
        // Removed after the animation rather than left on: it says "this is the
        // one you asked for", which stops being true the moment the reader
        // starts looking around.
        setTimeout(() => card.classList.remove('jump-target'), 2600);
    };

    const find = () => document.querySelector(`.card[data-id="${CSS.escape(wanted)}"]`);
    const already = find();
    if (already) { settle(already); return; }

    const observer = new MutationObserver(() => {
        const card = find();
        if (card) { observer.disconnect(); settle(card); }
    });
    observer.observe(document.body, { childList: true, subtree: true });
    // A card that never arrives is an ordinary outcome — the day may hold it for
    // a viewer whose filters differ, or the link may be old. Stop watching
    // rather than observing the document for the life of the page.
    setTimeout(() => observer.disconnect(), 15000);
}

document.addEventListener('DOMContentLoaded', highlightJumpTarget);
