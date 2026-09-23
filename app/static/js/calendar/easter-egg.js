// The hidden tracker's two ways in: a keyboard sequence and a run of taps on
// the version tag.
//
// NEITHER HAS A DECLARATIVE FORM. No markup, stylesheet rule or route can watch
// for ten keys pressed in an order, or for seven presses inside a second and a
// half; both are input with no control behind them, which is the whole point of
// them. And whether this browser has been let in before is `localStorage`,
// deliberately — a server-rendered flag would make one account's response
// differ from another's and say who has found it (see _nav_head.html).
//
// ONE LISTENER FOR EVERY PAGE THAT HAS ONE, and that is what makes this correct
// rather than merely tidier. The tracker used to carry a second copy of the
// sequence under a renamed variable — the rename being the tell — and the two
// listeners both survived a boosted navigation, so once the calendar had been
// visited the tracker had BOTH bound: the calendar's saw a `window` flag the
// calendar had left behind and navigated away from the page it was already on.
//
// SO THE ANSWER IS READ OFF THE PAGE, NOT OFF `window`. Every page states what it
// is on `#pageMeta`, inside the swapped region, so a boosted arrival brings its
// own copy (static/js/boost.js). `data-distrakt` is on that element only where
// there is somewhere to go, which the server resolves from the session — so the
// tracker never carries it, and a reveal cannot navigate away from the tracker
// however many pages have been visited.
//
// Both unlocks only lead anywhere for an account the server would actually let
// into the tracker. For everyone else the Konami code plays the sound and stops
// there, which reads as a self-contained joke rather than a locked door, and the
// footer tap does nothing at all — audio from a stray tap on a version number
// would startle someone who wasn't looking for an easter egg and give the game
// away in the process.

// Whether the page currently on screen has a tracker to reveal.
function distraktReachable() {
    const meta = document.getElementById('pageMeta');
    return !!(meta && meta.dataset.distrakt);
}

function revealSecret() {
    if (!distraktReachable()) return;
    // Remember that the easter egg has been used so the calendar can surface a
    // permanent Distrakt nav button on future visits.
    try { localStorage.setItem('distraktRevealed', '1'); } catch (e) {}
    location.href = '/distrakt';
}

// Once revealed, show the Distrakt nav button on the calendar. The inline head
// script already set this class from local storage before first paint, so this is
// only the in-session path — the reveal happening while the page is open. The
// class lives on <html>, which a boosted nav does not swap, so a freshly swapped
// nav element inherits the right visibility with nothing to re-apply.
function initDistraktNav() {
    if (!distraktReachable()) return;
    let revealed = false;
    try { revealed = localStorage.getItem('distraktRevealed') === '1'; } catch (e) {}
    if (revealed) document.documentElement.classList.add('has-distrakt');
}

const KONAMI_SEQUENCE = ['ArrowUp', 'ArrowUp', 'ArrowDown', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'ArrowLeft', 'ArrowRight', 'b', 'a'];
let konamiBuffer = [];

document.addEventListener('keydown', (e) => {
    konamiBuffer.push(e.key);
    konamiBuffer = konamiBuffer.slice(-KONAMI_SEQUENCE.length);
    if (konamiBuffer.length === KONAMI_SEQUENCE.length && konamiBuffer.every((k, i) => k === KONAMI_SEQUENCE[i])) {
        konamiBuffer = [];
        if (distraktReachable()) revealSecret();
        else new Audio('/static/audio/distrakt.mp3').play().catch(() => {});
    }
});

const BUILD_TAP_TARGET = 7;
const BUILD_TAP_WINDOW_MS = 1500;
let buildTapCount = 0;
let buildTapLast = 0;

// DELEGATED FROM THE DOCUMENT, like the keydown above and every primitive in
// static/js/ui.js. The version tag is inside the swapped region, so a boosted
// navigation replaces it with a fresh element — which this used to answer by
// being re-run on every arrival and guarding against binding the same element
// twice. A listener on the document is bound once and never has to know that the
// tag was replaced, so the guard, the re-binding and the init call all go.
//
// AND IT ASKS AT THE CLICK RATHER THAN AT INIT whether there is anywhere to go,
// which is the same question one moment later and cannot be left stale by a
// navigation that arrived after the binding.
//
// The counters are module scope on purpose: a streak survives a navigation.
document.addEventListener('click', (e) => {
    if (!(e.target instanceof Element) || !e.target.closest('.version-tag')) return;
    if (!distraktReachable()) return;
    const now = Date.now();
    buildTapCount = (now - buildTapLast > BUILD_TAP_WINDOW_MS) ? 1 : buildTapCount + 1;
    buildTapLast = now;
    if (buildTapCount >= BUILD_TAP_TARGET) revealSecret();
});
