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

// EVERY FETCH, IN ONE PLACE, because the rule is about all of them. Wrapping the
// call sites instead means each new one has to remember — which is exactly what
// happened: the bar was added for adding a show and then missing from removing
// one, from adding a film, and from the month load an ordinary refresh runs.
// A rule that every caller must uphold cannot live in the callers.
//
// NO FETCH IS EXEMPT ANY MORE. Two used to be: a card's season line, one per
// card as a month scrolled past, and the Sonarr/Radarr/Seerr status poll on its
// timer — neither something a person was waiting through. The line is an htmx
// request now that names itself as its own indicator (_card.html), and the
// server draws the library marks with the page, so nothing polls. Every fetch
// left is something somebody asked for.
//
// htmx REQUESTS DO NOT COME THROUGH HERE. It uses XMLHttpRequest and marks the
// bar itself through `hx-indicator`, so the two never double-count.
const nativeFetch = window.fetch.bind(window);

window.fetch = function (input, init) {
    pageBusy(true);
    // `finally` so a refused request releases the bar exactly as a served one
    // does — a failure is still the end of the waiting.
    return nativeFetch(input, init).finally(() => pageBusy(false));
};

function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
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
    // WHAT AN EARLIER ANSWER PUT IN THE HEADER GOES WITH IT. The calendar's
    // answer fills this slot out-of-band with the add-to-library buttons for its
    // own title; left standing, one card's buttons would sit over the next
    // card's panel while it loads, and over a refusal that has none.
    const actions = document.getElementById('detailsActions');
    if (actions) actions.replaceChildren();
    document.getElementById('detailsModal').classList.add('open');
    htmx.ajax('GET', url, { target: '#detailsBody', swap: 'innerHTML' });
}

function closeDetailsPanel() { closeModal('detailsModal'); }

// A CONTROL THAT ONLY OPENS THE PANEL IS MARKUP. `data-details-open` is the
// address the server built and `data-details-heading` the name to show while it
// loads, so an element carrying both needs no handler of its own. Every opener in
// the app is one: a rankings board row's ⓘ button, a calendar card and a share
// page's card.
//
// A CONTROL INSIDE AN OPENER ACTS FOR ITSELF. A card is an opener that also holds
// controls of its own — an outbound link, the watch toggle, a source flip, a
// badge's filter offer (a <details> with radios in it) — and a click on one of
// those is a click on THAT, which must not also open the panel behind it. So the
// click opens nothing when it landed on such a control inside the opener; the
// opener being a control itself, like the board row's button, still opens. This
// is the one statement of that rule: the cards used to carry their own opener
// functions, each re-stating it, and the badge a capture listener stopping the
// click before the card's inline handler could see it.
const ACTS_FOR_ITSELF = 'a, button, summary, label, input, select, textarea';

document.addEventListener('click', (e) => {
    if (!(e.target instanceof Element)) return;
    const opener = e.target.closest('[data-details-open]');
    if (!opener) return;
    const control = e.target.closest(ACTS_FOR_ITSELF);
    if (control && control !== opener && opener.contains(control)) return;
    openDetailsPanel(opener.dataset.detailsOpen, opener.dataset.detailsHeading || 'Details');
});

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


/* ---- The three shared markup blocks ----
 *
 * Each of these exists because a dozen call sites were writing the same four
 * lines, and each of them is the LAST piece of script a whole feature needs.
 * They are declared here, once, and used by markup anywhere in the app.
 */

// `data-copy="<selector>"` — copy that element's text.
//
// ONLY SCRIPT CAN WRITE THE CLIPBOARD, which is the whole of why this is here.
// What is not intrinsic is everything around it: six copy functions, one per
// thing worth copying, each reading its own element and raising its own toast.
// The selector names what to copy and the delegated listener does the rest.
//
// SELECTING THE TEXT IS THE FALLBACK, not an error message. A clipboard write
// can be refused — an insecure origin, a permission the browser did not grant —
// and leaving the text selected means the reader can still take it with the
// keyboard, which is what they were going to do anyway.
document.addEventListener('click', (e) => {
    if (!(e.target instanceof Element)) return;
    const trigger = e.target.closest('[data-copy]');
    if (!trigger) return;
    const source = document.querySelector(trigger.dataset.copy);
    if (!source) return;
    const text = source.value !== undefined ? source.value : source.textContent;
    navigator.clipboard.writeText(text).then(
        () => toast('Copied to clipboard', true),
        () => {
            if (source.select) source.select();
            toast('Could not copy — text selected instead', false);
        },
    );
});

// `data-file-into="<selector>"` — read the chosen file into that field, so a
// form can carry it.
//
// THE ONE THING ONLY A BROWSER CAN DO HERE, and the reason is the shape guard:
// app/authz.py admits `application/json` and nothing else, because a
// cross-origin multipart form post is exactly what that rule exists to refuse.
// So a file's contents have to reach the server as a FIELD of a JSON body, and
// only this side can read a local file. Everything around it is markup: the
// submit is the form's, the typed confirmation is its `required pattern`, and
// the answer is a fragment the server draws.
//
// `data-file-as="base64"` for a picture (the payload `user_images.add_image`
// takes), text otherwise. `data-file-name="<selector>"` fills a name field from
// the file's own name, which is the obvious first guess and is also something
// only this side knows. `data-file-status="<selector>"` is where an unreadable
// file is reported — said BEFORE anything else is asked of the reader, which
// beats typing a confirmation phrase and then finding out.
//
// `data-file-count="<key>:<noun>,…"` summarizes a JSON document the same way for
// every caller: the named top-level arrays are counted, entries sharing a noun
// are added together, and the sentence is one sentence rather than one per page.
async function readChosenFile(input) {
    const into = document.querySelector(input.dataset.fileInto);
    const status = input.dataset.fileStatus
        ? document.querySelector(input.dataset.fileStatus) : null;
    const file = input.files && input.files[0];
    if (into) into.value = '';
    if (!file) { if (status) status.textContent = ''; return; }
    const asBase64 = input.dataset.fileAs === 'base64';
    let text;
    try {
        text = asBase64 ? await fileAsBase64(file) : await file.text();
    } catch (e) {
        if (status) status.textContent = 'That file could not be read.';
        input.value = '';
        return;
    }
    if (into) into.value = text;
    const namer = input.dataset.fileName ? document.querySelector(input.dataset.fileName) : null;
    if (namer && !namer.value) {
        namer.value = file.name.replace(/\.[^.]+$/, '').slice(0, namer.maxLength || 40);
    }
    if (status) status.textContent = asBase64 ? '' : describeDocument(text, input.dataset.fileCount);
}

function fileAsBase64(file) {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        // Everything after the comma of a data: URL, which is what the upload
        // routes decode — the prefix names a media type they establish for
        // themselves from the bytes.
        reader.onload = () => resolve(String(reader.result).split(',').pop());
        reader.onerror = () => reject(new Error('unreadable'));
        reader.readAsDataURL(file);
    });
}

function describeDocument(text, spec) {
    let doc;
    try { doc = JSON.parse(text); } catch (e) { return "That file isn't readable JSON."; }
    const totals = new Map();
    for (const entry of (spec || '').split(',').filter(Boolean)) {
        const [key, noun] = entry.split(':');
        const rows = Array.isArray(doc[key]) ? doc[key].length : 0;
        totals.set(noun, (totals.get(noun) || 0) + rows);
    }
    if (!totals.size) return '';
    return 'Ready to restore ' +
        [...totals].map(([noun, n]) => `${n} ${noun}(s)`).join(' and ') + '.';
}

document.addEventListener('change', (e) => {
    if (e.target instanceof Element && e.target.dataset.fileInto) readChosenFile(e.target);
});

// `data-modal-open="<id>"` and `data-modal-close="<id>"` — show and hide an
// overlay.
//
// A CLASS TOGGLE IS NOT A REASON FOR A FUNCTION. Every modal in this app was
// opened by a one-line wrapper named after it, called from an inline handler;
// closing already had one shared implementation (closeModal, which tears down
// whatever the overlay was playing) and opening had a dozen. The id is the only
// thing that differed.
//
// FOCUS IS WHY OPENING IS NOT PURELY DECLARATIVE. `autofocus` applies when an
// element is PARSED, so it does nothing for markup that has been sitting hidden
// in the page since the load — only the browser can move focus into something
// being revealed, and a search box you have to click into first is the thing
// this is for.
document.addEventListener('click', (e) => {
    if (!(e.target instanceof Element)) return;
    const closer = e.target.closest('[data-modal-close]');
    if (closer) { closeModal(closer.dataset.modalClose); return; }
    const opener = e.target.closest('[data-modal-open]');
    if (!opener) return;
    const modal = document.getElementById(opener.dataset.modalOpen);
    if (!modal) return;
    modal.classList.add('open');
    const focus = modal.querySelector('[autofocus]');
    if (focus) focus.focus();
});

// `data-fill="<selector>"` with `data-fill-value` — write a value into a field,
// and `data-day-label="<selector>"` — keep a button naming the day that field
// holds.
//
// THE ONE THING ON THIS PAGE THAT IS GENUINELY THE BROWSER'S: a label that has
// to follow what somebody TYPES, before anything is submitted. Every day the
// server already knows about arrives spelled — the field's opening value is
// named in the button as drawn, and each preset carries its own `data-fill-label`
// — so this formats a date only when there is no server answer to use.
function fillField(field, value, label) {
    if (!field) return;
    field.value = value;
    field.dispatchEvent(new Event('input', { bubbles: true }));
    // AFTER the event, not before: the listener below would otherwise re-spell
    // the day this label was handed for.
    const button = label && field.dataset.dayLabel
        ? document.querySelector(field.dataset.dayLabel) : null;
    if (button) button.textContent = `${button.dataset.labelPrefix} ${label}`;
    field.focus();
}

document.addEventListener('click', (e) => {
    if (!(e.target instanceof Element)) return;
    const preset = e.target.closest('[data-fill]');
    if (preset) fillField(document.querySelector(preset.dataset.fill),
                          preset.dataset.fillValue || '', preset.dataset.fillLabel);
});

document.addEventListener('input', (e) => {
    if (!(e.target instanceof Element) || !e.target.dataset.dayLabel) return;
    const button = document.querySelector(e.target.dataset.dayLabel);
    if (!button) return;
    const value = e.target.value;
    // The button names the day it will use, so a date typed above it and a
    // button saying something else can never be two answers on screen at once.
    button.textContent = value
        ? `${button.dataset.labelPrefix} ${new Date(value + 'T00:00:00')
            .toLocaleDateString(undefined, { day: 'numeric', month: 'long', year: 'numeric' })}`
        : button.dataset.labelEmpty;
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
