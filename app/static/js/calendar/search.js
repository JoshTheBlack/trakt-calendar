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

// WHERE A JUMP WAS AIMED, once the month it lives on has painted.
//
// IT WAITS FOR THE TARGET RATHER THAN ASSUMING IT. A calendar ships its first few
// day blocks inline and fetches the rest when they are scrolled to, so a jump
// into a later month lands on a placeholder that has not become cards yet. The
// observer is what makes the arrival survive that; without it the target is
// found on some jumps and not others, depending entirely on how far down the
// month it happened to be. The browser's own `#day-` handling has exactly this
// problem and no way around it, which is why the anchor is honoured here too
// rather than left to the address bar.
//
// TWO TARGETS, ONE MECHANISM, DIFFERENT CLAIMS. `highlight=` names a CARD and is
// sent only by a stored result, where the calendar's own read path already
// confirmed that card will be drawn. A `#day-` anchor names a DAY and is what a
// catalogue result carries: it points at where the title should be without
// claiming anything is there. The card wins when both are present, being the
// more specific of the two.
function scrollToJumpTarget() {
    const wanted = new URLSearchParams(window.location.search).get('highlight');
    const day = (window.location.hash || '').startsWith('#day-')
        ? window.location.hash.slice(1) : '';
    if (!wanted && !day) { return; }

    const settle = (node, isCard) => {
        node.scrollIntoView({ block: 'center', behavior: 'smooth' });
        if (!isCard) { return; }
        node.classList.add('jump-target');
        // Removed after the animation rather than left on: it says "this is the
        // one you asked for", which stops being true the moment the reader
        // starts looking around.
        setTimeout(() => node.classList.remove('jump-target'), 2600);
    };

    const find = () => {
        if (wanted) {
            const card = document.querySelector(`.card[data-id="${CSS.escape(wanted)}"]`);
            if (card) { return [card, true]; }
        }
        if (day) {
            const block = document.getElementById(day);
            if (block) { return [block, false]; }
        }
        return null;
    };

    const already = find();
    if (already) { settle(already[0], already[1]); return; }

    const observer = new MutationObserver(() => {
        const hit = find();
        if (hit) { observer.disconnect(); settle(hit[0], hit[1]); }
    });
    observer.observe(document.body, { childList: true, subtree: true });
    // A target that never arrives is an ordinary outcome — the day may hold the
    // title for a viewer whose filters differ, the link may be old, or no source
    // may list it on that calendar at all. Stop watching rather than observing
    // the document for the life of the page.
    setTimeout(() => observer.disconnect(), 15000);
}
