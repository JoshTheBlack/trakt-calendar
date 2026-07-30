/* What a boosted navigation does NOT do for you, and this does.
 *
 * hx-boost is on every page's <body>, so a link swaps the body's CHILDREN and
 * nothing else. Two things a real navigation would have done are therefore left
 * undone, and both of them look like the destination page is broken:
 *
 *   1. THE <head> IS NEVER TOUCHED, so the page you arrive at never loads its own
 *      scripts. The stylesheet is one bundle and identical everywhere, which is
 *      what lets a boosted page land STYLED — but each page's scripts differ, so
 *      arriving at one you have not visited yet lands it INERT.
 *   2. THE <body> ELEMENT ITSELF SURVIVES, so its class attribute is still the
 *      previous page's. Those classes are not decoration: they carry the card
 *      style, the day packing, whether not-watching is hidden, and which shell
 *      layout the page wants.
 *
 * So every page states both facts on a #pageMeta element INSIDE its body, where a
 * swap will bring the new page's copy with it, and this wears them.
 *
 * READ FROM THE DOM, NOT FROM THE RESPONSE. The obvious implementation parses the
 * incoming HTML in htmx:beforeSwap, and it silently misses the Back button:
 * htmx restores a page from its history cache without a request, so there is no
 * response to read. Anything already in the document is there either way.
 *
 * NOTHING IS EVER LOADED TWICE. These are ordinary scripts sharing one global
 * scope (see assets.PAGE_SCRIPTS), so a second execution of one would re-declare
 * its top-level const and die with a SyntaxError, taking every function in it
 * with it. The set below is seeded from what the server rendered into this
 * page's <head> and grows as pages are visited, so a script is fetched the first
 * time it is needed and never again.
 */

// BACK RE-ASKS THE SERVER. htmx caches each page's body as you leave it and
// restores that snapshot on Back, with no request — and every page here is a view
// of server state that has moved on since. The rankings board is the sharp edge:
// its markup carries the board's VERSION, every save has to echo the current one,
// and a restored snapshot carries the version from before whatever you did on the
// way out. The next drag then saves against a stale version, gets the 409 that
// means "another tab changed this", and reloads — losing the drag and looking
// like a bug in saving rather than in going Back.
// A restore then costs one local request, which is what a real navigation cost.
htmx.config.historyCacheSize = 0;

// Which scripts each page needs, in load order, as URLs with their cache-busting
// token already on them. Rendered by the head macro from the one list in
// app/assets.py, so this file names no filenames of its own.
const PAGE_SCRIPTS = (() => {
    const node = document.getElementById('pageScripts');
    try {
        return node ? JSON.parse(node.textContent) : {};
    } catch (e) {
        console.error('the page script map is unreadable', e);
        return {};
    }
})();

// Everything already running in this document: what the server put in the <head>
// of the page that started the session, plus whatever has been added since.
// Keyed on the PATH, so the same file is not re-fetched under a different token.
const runningScripts = new Set(
    [...document.querySelectorAll('script[src]')].map(s => new URL(s.src, location.href).pathname));

// Each page's own init, registered by its boot.js as that file loads. Called on
// the cold load and again on every arrival, which is what makes a page reached
// for the second time behave like one just loaded.
const pageInits = new Map();

function registerPage(name, init) {
    pageInits.set(name, init);
}

// One <script src> appended to <head>, resolving when it has executed. async is
// forced OFF: a dynamically inserted script defaults to async, and a page's files
// depend on being executed in the order its list gives.
function loadScript(url) {
    return new Promise((resolve) => {
        const path = new URL(url, location.href).pathname;
        if (runningScripts.has(path)) { resolve(); return; }
        runningScripts.add(path);
        const el = document.createElement('script');
        el.src = url;
        el.async = false;
        el.addEventListener('load', () => resolve());
        el.addEventListener('error', () => {
            // Left in the set deliberately: retrying on every navigation would
            // hammer a file that is genuinely missing, and the page is already
            // as broken as it is going to get.
            console.error('could not load', url);
            resolve();
        });
        document.head.appendChild(el);
    });
}

async function startPage(name) {
    for (const url of PAGE_SCRIPTS[name] || []) {
        await loadScript(url);
    }
    const init = pageInits.get(name);
    if (init) init();
}

// The #pageMeta this has already acted on. Identity, not content: a FRAGMENT swap
// — a day block arriving, a tier's rows — replaces one container and leaves
// #pageMeta exactly where it was, so the same node means the same page and there
// is nothing to do. A whole-body swap brings a new one.
let currentMeta = null;

function arrived() {
    const meta = document.getElementById('pageMeta');
    if (!meta || meta === currentMeta) return;
    currentMeta = meta;
    // In the same task as the swap that brought it, so the browser paints the new
    // content and the classes that style it together.
    document.body.className = meta.dataset.bodyClass || '';
    if (meta.dataset.page) startPage(meta.dataset.page);
}

// Every way a page can arrive. afterSwap covers a boosted navigation,
// historyRestore covers Back and Forward (which htmx serves from its own cache,
// with no request and no swap event), and DOMContentLoaded covers a cold load —
// deferred scripts all execute before it, so every boot.js has registered by then.
document.addEventListener('htmx:afterSwap', arrived);
document.addEventListener('htmx:historyRestore', arrived);
document.addEventListener('DOMContentLoaded', arrived);
