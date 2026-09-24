/* What a boosted navigation does NOT do for you, and this does.
 *
 * hx-boost is on every signed-in page's <body>, so a link swaps the body's
 * CHILDREN and nothing else — no markup, stylesheet rule, template or route can
 * make a swap behave like a load. Two things a real navigation would have done
 * are therefore left undone, and both of them look like the destination page is
 * broken:
 *
 *   1. THE <body> ELEMENT ITSELF SURVIVES, so its class attribute is still the
 *      previous page's. Those classes are not decoration: they carry the card
 *      style, the day packing, whether not-watching is hidden, and which shell
 *      layout the page wants.
 *   2. NO DOMContentLoaded FIRES, so a page with something to start — the
 *      calendar's header measurement, the board's drag binding — is never told
 *      it has arrived.
 *
 * THE SCRIPTS THEMSELVES ARE NOT ONE OF THEM. Every page a boost can reach loads
 * the same files (app/assets.py's APP_SCRIPTS), so the <head> a swap leaves in
 * place is already the one the page arrived at would have loaded. The pages that
 * load fewer do not boost at all — templates/_base.html says why.
 *
 * So every page states what it is on a #pageMeta element INSIDE its body, where a
 * swap will bring the new page's copy with it (templates/_base.html draws it),
 * and this wears the classes and announces the arrival.
 *
 * READ FROM THE DOM, NOT FROM THE RESPONSE. The obvious implementation parses the
 * incoming HTML in htmx:before:swap, and it reads the swap's own payload rather
 * than the document that payload is about to become — two facts to keep in step
 * where one will do. Anything already in the document is there whatever brought
 * it, so every arrival is handled by one path.
 */

/* FIVE DEFAULTS TURNED BACK, and one behaviour this file no longer has to ask
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
// wrote. A boosted navigation that fails never reaches this at all: the server
// answers it with HX-Redirect to the same address (app/main.py), which htmx acts on
// before the status is looked at.
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

// NO SETTLE STEP. Before a swap htmx copies the OLD element's attributes onto every
// element in the answer that shares an id with one on the page, and puts the new
// ones back a tick later — so a CSS transition can run between the two. Nothing
// in this app's stylesheet transitions on that, and on a form field it is a
// defect: MEASURED in Firefox and Chromium, a display-name field redrawn with the
// name the server stored came back with the right `value` attribute and an EMPTY
// value on screen, and the same shape would blank a revealed key or a temporary
// password the second time one is drawn. A delay of nought skips the copy
// entirely; the answer is inserted exactly as the server wrote it.
htmx.config.defaultSettleDelay = 0;

// The #pageMeta this has already acted on. Identity, not content: a FRAGMENT swap
// — a day block arriving, a tier's rows — replaces one container and leaves
// #pageMeta exactly where it was, so the same node means the same page and there
// is nothing to do. A whole-body swap brings a new one.
let currentMeta = null;

// WEARING THE CLASSES AND STARTING THE PAGE ARE TWO QUESTIONS, because a new
// #pageMeta does not always mean a new PAGE. The calendar's view preferences —
// card style, day packing, hiding not-watching — are classes on <body>, and
// <body> is the one element a swap never replaces, so changing one is answered
// with this element alone, out of band. That is a new node carrying the same
// page, and re-starting the page on it would re-run an init nothing asked for.
//
// THE ANSWER SAYS WHICH IT IS RATHER THAN THIS GUESSING. `data-view-only` is on
// the copy a preference sends back (templates/_calendar_meta.html) and on no
// other. Telling them apart by whether the page NAME changed was tried and is
// WRONG: a boosted navigation from one month to the next carries the same name,
// so the page it arrived at would never be initialised.
//
// THE ARRIVAL IS AN EVENT, NOT A CALL. `page-arrived` names the page, and each
// page's boot.js listens for its own name — so this file knows no page's init,
// and no file has to have run before another for its init to be found.
function arrived() {
    const meta = document.getElementById('pageMeta');
    if (!meta || meta === currentMeta) return;
    currentMeta = meta;
    // In the same task as the swap that brought it, so the browser paints the new
    // content and the classes that style it together.
    document.body.className = meta.dataset.bodyClass || '';
    if (meta.dataset.page && !meta.hasAttribute('data-view-only')) {
        document.dispatchEvent(new CustomEvent('page-arrived', { detail: { page: meta.dataset.page } }));
    }
}

// Every way a page can arrive, and there are only two of them now. A swap covers
// the boosted navigation AND Back/Forward alike, because a restore is a real
// request answered by a real swap; DOMContentLoaded covers a cold load —
// deferred scripts all execute before it, so every boot.js is listening by then.
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
