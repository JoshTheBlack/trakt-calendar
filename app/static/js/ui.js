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
function closeModal(id) {
    const modal = document.getElementById(id);
    if (!modal) return;
    modal.classList.remove('open');
    modal.querySelectorAll('iframe').forEach(frame => frame.remove());
    // Native media, for anything that grows one later: pausing is enough, and
    // unlike an iframe it keeps its position if it is shown again.
    modal.querySelectorAll('video, audio').forEach(media => {
        try { media.pause(); } catch (e) { /* a detached element cannot pause */ }
    });
}


// A SELECT WHOSE VALUE IS PART OF THE URL MUST NOT SURVIVE A RESTORE SAYING
// OTHERWISE. `autocomplete="off"` in the markup handles the browser reapplying a
// control's value over a freshly parsed page; it has nothing to say about the
// two RESTORES that hand back a whole DOM:
//
//   - the back/forward cache, which returns the page exactly as it was left,
//     including the choice the visitor made a moment before navigating away;
//   - htmx's history cache, which serves a boosted Back from a snapshot and
//     makes no request at all.
//
// Both put a select on screen naming a view the page is not showing, and because
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

// htmx's own history restore, which is a different path from the bfcache and
// fires no pageshow at all.
document.body.addEventListener('htmx:historyRestore', resyncUrlStateSelects);
