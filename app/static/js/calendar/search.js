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

// WHERE A JUMP WAS AIMED, and STAYING there while the month finishes arriving.
//
// TWO PROBLEMS, AND THE SECOND IS THE ONE THAT MADE THIS UNRELIABLE.
//
// THE FIRST IS THAT THE TARGET MAY NOT EXIST YET. A calendar ships its first
// few day blocks and each later one fetches itself when it is scrolled to, so a
// jump into a later month lands on a placeholder. An observer covers that: the
// scroll waits for the node.
//
// THE SECOND IS THAT SCROLLING TO IT IS WHAT MAKES IT MOVE. Arriving at a day
// drags the days ABOVE it into view, each of them fetches itself, and each swap
// replaces a short placeholder with a tall block — so the target is pushed down
// by everything that loads above it, and a single scroll lands short. The more
// titles a month holds the further it drifts, which is exactly the report:
// sometimes the top of the right day, sometimes several days early.
//
// SO IT KEEPS CORRECTING UNTIL THE PAGE STOPS MOVING. Every mutation re-checks
// where the target actually is and scrolls again if it has moved; when nothing
// has moved it for a moment, the jump is done. That is a settling loop rather
// than one shot, and it is the only thing that survives content growing above
// the anchor after the fact.
//
// TWO TARGETS, ONE MECHANISM, DIFFERENT CLAIMS. `highlight=` names a CARD and is
// sent only by a stored result, where the calendar's own read path already
// confirmed that card will be drawn. A `#day-` anchor names a DAY and is what a
// catalogue result carries: it points at where the title should be without
// claiming anything is there. The card wins when both are present, being the
// more specific of the two.

// How long the page must hold still before a jump is called finished, and the
// longest it will keep trying. The first is a settle window, not a guess at
// network time; the second stops a month that never stops loading from holding
// the reader's scroll position for the life of the page.
const JUMP_SETTLED_MS = 500;
const JUMP_GIVE_UP_MS = 12000;

function scrollToJumpTarget() {
    const wanted = new URLSearchParams(window.location.search).get('highlight');
    const day = (window.location.hash || '').startsWith('#day-')
        ? window.location.hash.slice(1) : '';
    if (!wanted && !day) { return; }

    const find = () => {
        if (wanted) {
            const card = document.querySelector(`.card[data-id="${CSS.escape(wanted)}"]`);
            if (card) { return { node: card, isCard: true }; }
        }
        if (day) {
            const block = document.getElementById(day);
            if (block) { return { node: block, isCard: false }; }
        }
        return null;
    };

    const started = Date.now();
    let lastTop = null;
    let settledAt = null;
    let marked = false;
    let stopped = false;

    const stop = () => {
        if (stopped) { return; }
        stopped = true;
        observer.disconnect();
        clearInterval(ticker);
        for (const name of ['wheel', 'touchstart', 'keydown', 'pointerdown']) {
            window.removeEventListener(name, stop);
        }
    };

    const step = () => {
        if (stopped) { return; }
        if (Date.now() - started > JUMP_GIVE_UP_MS) { stop(); return; }
        const hit = find();
        if (!hit) { return; }  // still a placeholder; the observer will bring it

        const top = Math.round(hit.node.getBoundingClientRect().top);
        if (lastTop === null || Math.abs(top - lastTop) > 2) {
            // It moved — or this is the first sighting. Chase it. `auto` rather
            // than `smooth` after the first pass: a smooth scroll that is
            // re-issued mid-animation fights itself and lands somewhere neither
            // call asked for.
            hit.node.scrollIntoView({
                block: 'center',
                behavior: lastTop === null ? 'smooth' : 'auto',
            });
            lastTop = Math.round(hit.node.getBoundingClientRect().top);
            settledAt = Date.now();
            return;
        }

        // MARKED ONLY ONCE IT HAS STOPPED MOVING, so the highlight is not
        // running its animation while the reader is still being scrolled around.
        if (!marked && hit.isCard) {
            marked = true;
            hit.node.classList.add('jump-target');
            // Removed after the animation rather than left on: it says "this is
            // the one you asked for", which stops being true the moment the
            // reader starts looking around.
            setTimeout(() => hit.node.classList.remove('jump-target'), 2600);
        }
        if (settledAt !== null && Date.now() - settledAt > JUMP_SETTLED_MS) { stop(); }
    };

    // THE OBSERVER CATCHES ARRIVALS AND THE TICKER CATCHES EVERYTHING ELSE.
    // A day block swapping in fires a mutation, but an image finishing its load
    // resizes a card without one, and that moves the target just as far.
    const observer = new MutationObserver(step);
    const ticker = setInterval(step, 120);
    observer.observe(document.body, { childList: true, subtree: true });
    // THE READER OUTRANKS THE JUMP. A loop that keeps re-centring is exactly
    // what makes the landing reliable, and exactly what would fight somebody
    // who has started looking around before it settled — so the first sign of
    // that ends it. `passive` because none of these are being prevented; this
    // only wants to know they happened.
    for (const name of ['wheel', 'touchstart', 'keydown', 'pointerdown']) {
        window.addEventListener(name, stop, { passive: true });
    }
    step();
}
