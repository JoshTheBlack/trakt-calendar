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
 * incoming HTML in htmx:before:swap, and it reads the swap's own payload rather
 * than the document that payload is about to become — two facts to keep in step
 * where one will do. Anything already in the document is there whatever brought
 * it, so every arrival is handled by one path.
 *
 * NOTHING IS EVER LOADED TWICE. These are ordinary scripts sharing one global
 * scope (see assets.PAGE_SCRIPTS), so a second execution of one would re-declare
 * its top-level const and die with a SyntaxError, taking every function in it
 * with it. The set below is seeded from what the server rendered into this
 * page's <head> and grows as pages are visited, so a script is fetched the first
 * time it is needed and never again.
 */

/* FOUR DEFAULTS TURNED BACK, and one behaviour this file no longer has to ask
 * for. Each flag below restores the behaviour the app's markup and routes were
 * written against; each is an exact-parity setting rather than a position, and
 * each becomes deletable once the markup it protects says the same thing for
 * itself. They are set here, at the top of the first script to run after htmx
 * itself, because htmx reads its config when the document finishes parsing and
 * deferred scripts all execute before that.
 */

// BACK RE-ASKS THE SERVER, AND THAT IS NOW THE FRAMEWORK'S OWN BEHAVIOUR — which
// is why there is a paragraph here and no line under it. The reason still binds:
// a Back that replayed a stored snapshot would hand back a page of server state
// that has moved on since. The rankings board is the sharp edge — its markup
// carries the board's VERSION, every save has to echo the current one, and a
// snapshot carries the version from before whatever you did on the way out. The
// next drag then saves against a stale version, gets the 409 that means "another
// tab changed this", and reloads: the drag is lost and it looks like a bug in
// saving rather than in going Back. A restore costs one local request instead,
// which is what a real navigation cost anyway. If a future version offers to
// cache history again, this is the paragraph that says not to take it.

// ATTRIBUTE INHERITANCE. hx-boost and hx-indicator are declared once on <body>
// and reach every link, form and fragment beneath them, in sixteen templates;
// hx-target and hx-sync do the same over the calendar's search controls. htmx
// now wants the parent to opt in per attribute (`hx-boost:inherited`), and the
// failure without this is silent in the worst way: nothing throws, links simply
// stop boosting and the progress bar stops lighting. The flag is exact parity
// and costs one line; converting sixteen templates is a separate change to make
// deliberately, not a side effect of taking a new version.
htmx.config.implicitInheritance = true;

// A 4xx OR 5xx IS NOT CONTENT. htmx now swaps every response but 204 and 304,
// which would paint a session-expired page into a single day cell and an error
// page's body into a document still wearing this page's <head>. Every fragment
// route here answers 200 with a rendered gap when it has nothing — a failure is
// something the server draws, not something the browser infers from a status —
// so a non-200 reaching the swap is by definition a response no template here
// wrote. The head's htmx:response:error handler turns the boosted case into a
// real navigation; this is what stops the swap it would race.
htmx.config.noSwap = [204, 304, '4xx', '5xx'];

// NO CLOCK ON A REQUEST. htmx now aborts one after sixty seconds. Filling a
// calendar month nobody has asked for yet is a long sequence of paced upstream
// calls behind one request, and it is the exact request that most deserves to
// finish: aborting it discards work already spent and leaves the month unstored,
// so the next visit starts over. The page-wide progress bar exists to say that
// wait is real and expected. The upstream transports carry their own per-call
// deadlines, which is where a hung service is actually caught.
htmx.config.defaultTimeout = 0;

// AN OUT-OF-BAND-ONLY RESPONSE STILL SWAPS. The pool's last page is exactly
// that: its rows travel out-of-band into the list and the response carries no
// replacement sentinel, so the empty main swap IS the answer — it removes the
// sentinel that asked and ends the chain. htmx now skips a main swap it would
// have nothing to put in, which would leave "Loading more…" on screen for a pool
// that has no more.
htmx.config.allowEmptySwapAfterOOB = true;

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

// Every way a page can arrive, and there are only two of them now. A swap covers
// the boosted navigation AND Back/Forward alike, because a restore is a real
// request answered by a real swap; DOMContentLoaded covers a cold load —
// deferred scripts all execute before it, so every boot.js has registered by then.
document.addEventListener('htmx:after:swap', arrived);
document.addEventListener('DOMContentLoaded', arrived);

// WHAT A SWAP PUT ON THE PAGE, for the two pages that have to finish a fragment
// off by hand — a day block's marks, a tier's sortable containers. Null for a
// whole-page arrival, which is this file's business and not theirs.
//
// IT IS NOT ONE PROPERTY, and that is the whole reason this lives in one place.
// The swap context names the element that was AIMED AT, which is right while the
// target survives the swap — an innerHTML swap — and wrong the moment it does
// not: a fragment replacing its own placeholder leaves that name pointing at the
// detached placeholder.
//
// WHAT REPLACED IT IS WHERE htmx DISPATCHED THE EVENT, AND THAT IS A NODE, NOT
// NECESSARILY AN ELEMENT. Measured in the browser rather than read: an outerHTML
// swap re-points the request at the FIRST NODE IT INSERTED, and every fragment
// this app renders begins with template whitespace, so that node is a text node —
// a day block, a pool page and a ranker row all arrived as "\n". Answering with it
// handed callers something that is not an element, and the calendar's day blocks
// quietly skipped everything that is meant to happen when one arrives. So a text
// node is stepped past to the element it precedes, which is what the fragment
// actually drew. Where the answer inserted no element at all (the pool's last
// page, whose rows all travel out-of-band) htmx dispatches on the container the
// placeholder sat in, which is an element already and is answered as it is.
function swappedContent(event) {
    const ctx = event.detail && event.detail.ctx;
    if (!ctx || ctx.target === document.body) return null;
    if (ctx.target && ctx.target.isConnected) return ctx.target;
    const landed = event.target;
    if (landed instanceof Element) return landed;
    if (landed instanceof Node) return landed.nextElementSibling || landed.parentElement;
    return null;
}
