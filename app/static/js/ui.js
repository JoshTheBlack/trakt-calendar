/* The three UI primitives every page uses, in one place.
 *
 * They used to be a copy per page script: app.js, distrakt.js, ranker.js and
 * share.js each carried their own esc(), three of them their own toast(), and
 * each copy carried a comment explaining that a page loads its own script rather
 * than the calendar's. That reason is gone — every page's <head> is built from
 * one list of assets, so one more entry costs a page nothing, while a fourth copy
 * of an escaper costs a reader four places to check when one of them is wrong.
 *
 * confirmInline arrived from nav.js, which is the site HEADER's behaviour and was
 * only holding this because it happened to be the file every page loaded.
 */

// ---- "Working on it" for the things htmx does not drive --------------------
// The page's progress bar is marked by htmx itself for anything htmx requests
// (`hx-indicator` on <body>, inherited by everything under it). A hand-written
// fetch is invisible to that, and some of those are the slowest actions in the
// app: adding a season asks a service for its episode list and then recomputes
// the whole month, which can run to several seconds with nothing on screen.
//
// COUNTED, NOT A FLAG. Two overlapping requests would otherwise have the first
// to finish clear a bar the second still needs, and the reader would watch it
// flicker off while the page was still working.
//
// A SEPARATE CLASS FROM htmx's OWN, so the two cannot fight: htmx adds and
// removes `htmx-request` on its own schedule, and a shared class would let one
// of them clear the other's state. The stylesheet shows the bar for either.
let pageBusyDepth = 0;

function pageBusy(busy) {
    const bar = document.getElementById('pageProgress');
    if (!bar) { return; }
    pageBusyDepth = Math.max(0, pageBusyDepth + (busy ? 1 : -1));
    bar.classList.toggle('is-busy', pageBusyDepth > 0);
}

// REQUESTS THAT SAY NOTHING ABOUT WHETHER THE PAGE IS BUSY, and the only two.
// A card's season summary fires once per card as the calendar is scrolled, and
// the integrations status polls on a timer; neither is anybody waiting on, and
// a bar that lit for them would be up for most of a scroll. Everything else is
// something a person asked for and is now waiting through.
const QUIET_REQUESTS = [/^\/api\/tile\b/, /^\/api\/integrations\//];

// EVERY FETCH, IN ONE PLACE, because the rule is about all of them. Wrapping the
// call sites instead means each new one has to remember — which is exactly what
// happened: the bar was added for adding a show and then missing from removing
// one, from adding a film, and from the month load an ordinary refresh runs.
// A rule that every caller must uphold cannot live in the callers.
//
// htmx REQUESTS DO NOT COME THROUGH HERE. It uses XMLHttpRequest and marks the
// bar itself through `hx-indicator`, so the two never double-count.
const nativeFetch = window.fetch.bind(window);

window.fetch = function (input, init) {
    let path = '';
    try {
        const raw = typeof input === 'string' ? input : (input && input.url) || '';
        path = new URL(raw, window.location.href).pathname;
    } catch (e) { /* a shape we cannot read is not a reason to refuse the call */ }
    if (QUIET_REQUESTS.some(pattern => pattern.test(path))) {
        return nativeFetch(input, init);
    }
    pageBusy(true);
    // `finally` so a refused request releases the bar exactly as a served one
    // does — a failure is still the end of the waiting.
    return nativeFetch(input, init).finally(() => pageBusy(false));
};

function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// The 11-character video id out of any of the URL shapes Trakt returns. Three
// details modals embed a trailer — the calendar's, the tracker's and the share
// page's — and each carried its own copy of this regex, one of them renamed only
// to avoid a name clash. Now that a page's scripts stay loaded and a later page's
// are added beside them, two copies of one function under one name is the last
// one loaded quietly winning.
function youTubeId(url) {
    const m = String(url).match(/(?:youtube\.com\/(?:watch\?(?:.*&)?v=|embed\/|v\/)|youtu\.be\/)([\w-]{11})/);
    return m ? m[1] : null;
}

function toast(message, ok) {
    let host = document.getElementById('toastHost');
    if (!host) { host = document.createElement('div'); host.id = 'toastHost'; document.body.appendChild(host); }
    const t = document.createElement('div');
    t.className = 'toast ' + (ok ? 'ok' : 'err');
    t.textContent = message;
    host.appendChild(t);
    while (host.children.length > 6) host.firstChild.remove();  // don't flood on bulk add
    requestAnimationFrame(() => t.classList.add('show'));
    setTimeout(() => { t.classList.remove('show'); setTimeout(() => t.remove(), 300); }, 4200);
}

// Replaces native confirm() dialogs everywhere in the app. Anchors a small
// "are you sure" popover under whichever button triggered it (position: fixed,
// so it works the same whether that button sits in a tight pill row, a table
// row, or a settings panel — no per-caller layout to get right) and animates it
// in instead of blocking the page with a browser-native dialog. Dismissed by
// Cancel, by clicking outside it, by scrolling, or by Escape.
function confirmInline(trigger, message, onConfirm, opts) {
    if (!trigger) return;
    const existing = document.querySelector('.inline-confirm');
    if (existing) {
        const wasForThisTrigger = existing._trigger === trigger;
        existing._dismiss();
        if (wasForThisTrigger) return;  // a second click on the same button just cancels it
    }

    const danger = !!(opts && opts.danger);
    const pop = document.createElement('div');
    pop.className = 'inline-confirm' + (danger ? ' danger' : '');
    // `message` can carry a username, an invite label, or a server error string
    // — none of it trusted — so it goes in as a text node, never as markup. Only
    // the two buttons, which contain no interpolation, are built via innerHTML.
    const msg = document.createElement('span');
    msg.className = 'hint';
    msg.textContent = message;
    const actions = document.createElement('div');
    actions.className = 'inline-confirm-actions';
    actions.innerHTML =
        '<button type="button" class="btn-ghost small">Cancel</button>' +
        '<button type="button" class="btn-primary small">Confirm</button>';
    pop.appendChild(msg);
    pop.appendChild(actions);
    document.body.appendChild(pop);

    const place = () => {
        const r = trigger.getBoundingClientRect();
        const width = pop.offsetWidth;
        pop.style.top = Math.round(r.bottom + 6) + 'px';
        const left = Math.min(
            Math.max(8, r.left),
            document.documentElement.clientWidth - width - 8,
        );
        pop.style.left = Math.round(left) + 'px';
    };
    place();
    requestAnimationFrame(() => pop.classList.add('show'));

    const dismiss = () => {
        document.removeEventListener('click', onOutside, true);
        document.removeEventListener('scroll', dismiss, true);
        document.removeEventListener('keydown', onKey, true);
        pop.classList.remove('show');
        setTimeout(() => pop.remove(), 160);
    };
    const onOutside = (e) => { if (!pop.contains(e.target) && e.target !== trigger) dismiss(); };
    const onKey = (e) => { if (e.key === 'Escape') dismiss(); };
    pop.querySelector('.btn-ghost').addEventListener('click', dismiss);
    pop.querySelector('.btn-primary').addEventListener('click', () => { dismiss(); onConfirm(); });
    // Deferred so the click that opened this popover doesn't immediately close it.
    setTimeout(() => {
        document.addEventListener('click', onOutside, true);
        document.addEventListener('scroll', dismiss, true);
        document.addEventListener('keydown', onKey, true);
    }, 0);

    pop._trigger = trigger;
    pop._dismiss = dismiss;
}

// Close a modal, and STOP WHATEVER IT WAS PLAYING.
//
// THE BUG THIS EXISTS FOR: the details modal embeds a trailer in an iframe, and
// closing it only removed the `open` class — which hides the modal and leaves the
// iframe loaded, so a trailer went on playing, audible, over a calendar with
// nothing on screen to pause. It stopped only when another modal replaced the
// markup, or on a reload, or when the video reached its end.
//
// HERE RATHER THAN IN EACH CLOSER because there are three of them — the calendar,
// the share page and the tracker each open a details modal, all three build the
// same trailer block, and all three had the same fault. Fixing it where they
// already share code is what stops the next modal inheriting it.
//
// THE FRAME HAS TO GO, NOT JUST ITS `src`. Hiding an iframe does not stop it and
// nor does anything CSS can do to it — the document inside goes on running. The
// obvious next move, clearing the src, does not stop it either: checked in the
// browser, the attribute reads back as null while the trailer is still audible,
// because dropping the attribute does not tear down the document already loaded.
// Removing the ELEMENT does. The modal body is rebuilt from the payload every
// time one opens, so there is nothing here worth keeping.
// WHAT ONE MODAL NEEDS DOING BESIDES CLOSING, registered by the file that owns
// it. Five of them have something: a form to reset, a handshake to stop, a timer
// to clear, a question to answer, a pending choice to forget.
//
// A REGISTRY RATHER THAN A LIST OF CLOSE FUNCTIONS, and the difference is which
// side knows. A list here would have to name functions that may not exist on the
// page being looked at — the Settings panel is admin-only, and calling its close
// function unconditionally is what used to throw for everybody else. A hook is
// registered BY the file that defines it, so it exists exactly when its modal
// does, and every way of dismissing that modal runs it without having to know it
// is there.
const modalCloseHooks = new Map();

function onModalClose(id, fn) {
    modalCloseHooks.set(id, fn);
}

function closeModal(id) {
    const modal = document.getElementById(id);
    if (!modal) return;
    // BEFORE the class comes off, so a hook can still read what is on screen.
    const hook = modalCloseHooks.get(id);
    if (hook) hook();
    modal.classList.remove('open');
    modal.querySelectorAll('iframe').forEach(frame => frame.remove());
    // Native media, for anything that grows one later: pausing is enough, and
    // unlike an iframe it keeps its position if it is shown again.
    modal.querySelectorAll('video, audio').forEach(media => {
        try { media.pause(); } catch (e) { /* a detached element cannot pause */ }
    });
}

// EVERY OVERLAY THAT IS OPEN, CLOSED PROPERLY. Driven off the class rather than a
// list of close functions, which is the shape the calendar's Escape handler was
// written in and for a good reason: the Settings modal is only rendered for
// admins, so calling closeSettings() unconditionally threw for everybody else and
// took the rest of the handler down with it. What that shape LOST was the
// teardown — it stripped the class by hand, so a trailer went on playing behind a
// box that had disappeared. Going through closeModal keeps the reason and fixes
// the omission, and it is the only place that has to know what closing means.
function closeOpenModals() {
    document.querySelectorAll('.modal-overlay.open').forEach(m => closeModal(m.id));
}

// ESCAPE IS ONE RULE FOR THE WHOLE APP, so it is stated once, here, beside the
// function that defines what closing is. It used to be four listeners — the
// calendar's, the site header's, the share page's and the ranker's — of which
// three closed a different subset and only one of them tore anything down. A page
// with something else to do on Escape still adds its own listener for THAT (the
// calendar stops its device-authorization polling, the ranker resolves the
// question it was asking); none of them has to close anything any more.
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeOpenModals();
});

// CLICKING THE BACKDROP CLOSES THE MODAL, ON EVERY MODAL. Eight of the fourteen
// carried an inline click handler on the overlay that compared the event's target
// with the element itself, and six carried nothing — so whether a stray click
// dismissed a panel depended on which panel it was. That is not a behaviour
// anybody chose; it is one that was remembered eight times and forgotten six.
//
// THE TARGET IS THE OVERLAY ITSELF, which is what distinguishes the backdrop from
// the box: a click inside the dialog has one of its children as the target and
// only reaches the overlay by bubbling. That is the same test each of the eight
// inline copies was making, stated once.
document.addEventListener('click', (e) => {
    const overlay = e.target;
    if (overlay instanceof Element && overlay.classList.contains('modal-overlay')
        && overlay.classList.contains('open')) {
        closeModal(overlay.id);
    }
});


/* ---- The details panel ----
 *
 * ONE PANEL AND ONE WAY OF OPENING IT. templates/_details_modal.html is included
 * by the calendar, the tracker and the public share page, and what goes inside it
 * is a fragment the server renders (templates/_details.html). Three pages each
 * built that markup for themselves, from the same payload, and the three copies
 * had drifted twice over — so what is left on this side is only the part a
 * fragment cannot do.
 *
 * WHICH IS THE HEADING, AND IT IS NOT AN OVERSIGHT THAT IT IS STILL HERE. The
 * panel opens the instant it is clicked and the answer arrives afterwards, so the
 * name at the top has to be right while the request is still in flight — it comes
 * from the card on screen, which is also the only thing that knows it: which
 * service answers decides what is in the payload, and one of them stores its
 * catalogue records without a title at all.
 *
 * THE TWO STATES WITH NO RESPONSE BEHIND THEM ARE CLONED, not built. They are
 * <template> elements in the shell, so every piece of markup this panel can show
 * is written in a template and none of it is assembled from strings here.
 */
function detailsPanelState(id) {
    const source = document.getElementById(id);
    return source ? source.content.cloneNode(true) : document.createDocumentFragment();
}

function openDetailsPanel(url, heading) {
    const body = document.getElementById('detailsBody');
    if (!body) return;
    document.getElementById('detailsTitle').textContent = heading;
    body.replaceChildren(detailsPanelState('detailsPending'));
    document.getElementById('detailsModal').classList.add('open');
    htmx.ajax('GET', url, { target: '#detailsBody', swap: 'innerHTML' });
}

function closeDetailsPanel() { closeModal('detailsModal'); }

// A REQUEST THAT ENDED WITHOUT PUTTING ANYTHING THERE left the panel on its
// loading line for ever, and that covers every failure the server could not draw
// for itself: a dropped connection, a session that lapsed into a redirect, the
// public page's rate limit. Everything the server CAN state it states as a
// fragment at 200, because a non-200 is never swapped (static/js/boost.js's
// `noSwap`).
//
// THE TEST IS WHETHER THE PLACEHOLDER SURVIVED, not what the request context
// says, and that is a measured correction rather than a preference: htmx runs
// its swap STEP for a no-swap response as well, so the context comes back
// reading "swapped" for a 404 whose swap was a no-op. Asking the DOM what is
// actually on the page is one question with one answer, and it does not depend
// on how the library spells its own progress.
//
// A SEASON TAB'S PANE IS THE SAME QUESTION ONE LEVEL IN. Each tab fetches its own
// season into its own pane, which starts out holding the same pending marker, so
// this asks about whatever inside the panel was aimed at rather than about the
// panel alone. `ctx.target` is the element AIMED AT, which is what is wanted here
// and is exactly right for these two swaps because both are innerHTML — a swap
// that REPLACES its target would need boost.js's `swappedContent` instead, and
// nothing here walks what a swap produced.
//
// `:scope >` IS LOAD-BEARING AND IT IS NOT A TIDINESS. The question is whether
// THE PLACEHOLDER THIS REQUEST PUT THERE is still there, and a placeholder is
// always the swap target's own immediate child. A descendant search asks a
// different question, and the difference is a defect that shipped: a panel with
// season tabs arrives holding one pending marker per UNOPENED season, so a
// perfectly good answer was read as "nothing landed" and replaced with the
// failure line. Anything with more than one season could not be opened at all.
document.addEventListener('htmx:finally:request', (event) => {
    const ctx = event.detail && event.detail.ctx;
    const target = ctx && ctx.target;
    if (!target || !target.closest || !target.closest('#detailsBody')) return;
    if (!target.querySelector(':scope > [data-details-pending]')) return;
    target.replaceChildren(detailsPanelState('detailsUnreachable'));
});


// A SELECT WHOSE VALUE IS PART OF THE URL MUST NOT SURVIVE A RESTORE SAYING
// OTHERWISE. `autocomplete="off"` in the markup handles the browser reapplying a
// control's value over a freshly parsed page; it has nothing to say about the
// two RESTORES that hand back a whole DOM:
//
//   - the back/forward cache, which returns the page exactly as it was left,
//     including the choice the visitor made a moment before navigating away.
//
// A BOOSTED Back IS NO LONGER ONE OF THEM, and that is why only one listener
// hangs off this. htmx re-asks the server for the page being returned to and
// swaps its answer in, so the select that arrives is the one the server rendered
// for that address — there is nothing left over to correct. The bfcache is a
// browser restore with no request behind it and still needs this.
//
// It puts a select on screen naming a view the page is not showing, and because
// these selects act on `change`, picking that same entry back fires no event —
// so that view becomes unreachable until a third one is chosen first. The share
// page shows it plainly: Back leaves the wrong endpoint named, and only F5
// clears it.
//
// OPT-IN VIA data-url-state, NOT EVERY SELECT ON THE PAGE. The calendar's card
// style and day packing are saved preferences that change nothing about the URL,
// and a restored page showing the visitor's own saved choice is CORRECT — this
// would revert it. The rule is narrow on purpose: it is for a control whose value
// is a fact about the address bar.
function resyncUrlStateSelects() {
    document.querySelectorAll('select[data-url-state]').forEach(select => {
        // The `selected` ATTRIBUTE, which is what the server rendered, rather
        // than the current value, which is what a restore may have overwritten.
        // `defaultSelected` reflects the attribute and no user interaction
        // changes it, so this is the page's own answer even on a restored DOM.
        const declared = Array.from(select.options).find(o => o.defaultSelected);
        if (declared && select.value !== declared.value) select.value = declared.value;
    });
}

window.addEventListener('pageshow', event => {
    // `persisted` is the bfcache restore. The plain load case needs nothing —
    // the markup is what it says — but running it anyway would cost a query
    // selector on every page for no reason.
    if (event.persisted) resyncUrlStateSelects();
});
