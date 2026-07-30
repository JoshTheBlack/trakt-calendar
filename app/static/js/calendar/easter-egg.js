// ---- Hidden /distrakt reveal: Konami code + footer build-tap (kept independent) ----
// Both unlocks only lead anywhere for an account the server would actually let
// into the tracker. For everyone else the Konami code plays the sound and stops
// there, which reads as a self-contained joke rather than a locked door, and the
// footer tap does nothing at all — audio from a stray tap on a version number
// would startle someone who wasn't looking for an easter egg and give the game
// away in the process.
function revealSecret() {
    if (!window.DISTRAKT_AVAILABLE) return;
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
    if (!window.DISTRAKT_AVAILABLE) return;
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
        if (window.DISTRAKT_AVAILABLE) revealSecret();
        else new Audio('/static/audio/distrakt.mp3').play().catch(() => {});
    }
});

const BUILD_TAP_TARGET = 7;
const BUILD_TAP_WINDOW_MS = 1500;
let buildTapCount = 0;
let buildTapLast = 0;

// The version tag is inside the swapped region, so a boosted nav replaces it with
// a fresh element; the dataset guard keeps a single tap listener per element (the
// tap counters live at module scope, so a streak survives across a nav).
function initBuildTap() {
    if (!window.DISTRAKT_AVAILABLE) return;
    const tag = document.querySelector('.version-tag');
    if (!tag || tag.dataset.tapBound) return;
    tag.dataset.tapBound = '1';
    tag.addEventListener('click', () => {
        const now = Date.now();
        buildTapCount = (now - buildTapLast > BUILD_TAP_WINDOW_MS) ? 1 : buildTapCount + 1;
        buildTapLast = now;
        if (buildTapCount >= BUILD_TAP_TARGET) revealSecret();
    });
}
