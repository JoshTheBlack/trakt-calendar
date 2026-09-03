// ---- Calendar search ----
// ALMOST NONE OF THIS FEATURE IS HERE, deliberately. Every request the search
// panel makes is an `hx-get` declared on the control that makes it — typing,
// Enter, and both widening buttons — so the debounce, the request replacement
// and the swap are markup rather than code. What is left is the two things a
// browser has to be asked in script: opening the panel, and finding a card once
// the day it sits on has painted.

function openCalendarSearch() {
    document.getElementById('searchModal').classList.add('open');
    const input = document.getElementById('searchInput');
    input.focus();
    input.select();
}

function closeCalendarSearch() {
    document.getElementById('searchModal').classList.remove('open');
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
