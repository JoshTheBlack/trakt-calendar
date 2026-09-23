/* The primitives every page uses, in one place, and each of them is here because
 * it is something only a browser can do: write the clipboard, read a local file,
 * put a dialog in the top layer, follow what somebody is typing.
 *
 * THEY USED TO BE A COPY PER PAGE SCRIPT: app.js, distrakt.js, ranker.js and
 * share.js each carried their own escaper, three of them their own toast, and
 * each copy carried a comment explaining that a page loads its own script rather
 * than the calendar's. That reason is gone — every page's <head> is built from
 * one list of assets, so one more entry costs a page nothing, while a fourth copy
 * of a thing costs a reader four places to check when one of them is wrong.
 *
 * TWO OF THEM HAVE SINCE LEFT ALTOGETHER, and what replaced them says what this
 * file is for. `esc()` went because nothing builds markup out of strings any
 * more — every view this app draws is a template. `confirmInline` measured a
 * button with getBoundingClientRect and placed a popover by hand; every "are you
 * sure" in the app is templates/_confirm.html now, which is a checkbox, two
 * labels and a stylesheet rule.
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
// htmx REQUESTS COME THROUGH HERE AND MUST NOT BE COUNTED, which is a correction
// rather than a precaution: htmx 3 and earlier used XMLHttpRequest, so this
// wrapper never saw one, and htmx 4 issues every request through `window.fetch`
// — it reads the property at call time (`ctx.fetch ||= window.fetch.bind(window)`,
// read in the vendored source), so it picks up whatever is installed here.
// MEASURED in Firefox 155 and Chromium 153 against a fragment carrying
// `hx-indicator="this"`: the page-wide bar lit for it. That is exactly what
// `hx-indicator` on a fragment exists to prevent — a card's season line asks for
// itself as the month scrolls past, and _card.html says in so many words that the
// page bar is for something a person is waiting through and a line per card is
// not. htmx marks its own requests through `hx-indicator`, so letting one through
// here counted it twice and overrode the narrower answer.
// THE HEADER IS THE TELL: htmx sets `HX-Request: true` on every request it makes.
const nativeFetch = window.fetch.bind(window);

window.fetch = function (input, init) {
    if (init && init.headers && init.headers['HX-Request']) return nativeFetch(input, init);
    pageBusy(true);
    // `finally` so a refused request releases the bar exactly as a served one
    // does — a failure is still the end of the waiting.
    return nativeFetch(input, init).finally(() => pageBusy(false));
};

// A LINE THIS SIDE RAISES, AND IT IS THE SERVER'S MARKUP. Every caller is a
// request that answers no markup to carry one — the layout save and the board
// read, which are JSON by design; the export, whose answer is a FILE — or the
// clipboard, which is no request at all. So the line has to be put on the page
// from here, and what it LOOKS like is still one fact: templates/_toast.html
// draws it, the <head> carries a <template> of it, and this fills that in.
// Assembling one here is what let the two drift, and they had: the script's
// carried no `role`, so every refusal went unannounced.
//
// NO TIMERS. `.is-flash` is a 4.2s CSS animation that fades the line in and back
// out on its own (style.css), which is how the server's toasts have always
// worked; this used to add a class on the next frame and take it away on a pair
// of setTimeouts, saying the same 4.2 seconds a second time.
//
// NOR A CAP. Six at once was the limit a bulk add needed, enforced by removing
// the oldest — which only ever applied to the lines THIS side made, and dropped
// an Undo offer as readily as a report. The stylesheet says it now, for both
// kinds and sparing an offer.
function toast(message, ok) {
    let host = document.getElementById('toastHost');
    if (!host) { host = document.createElement('div'); host.id = 'toastHost'; document.body.appendChild(host); }
    const line = document.getElementById('toastLine')
        .content.firstElementChild.cloneNode(true);
    line.classList.toggle('ok', !!ok);
    line.classList.toggle('err', !ok);
    line.textContent = message;
    host.appendChild(line);
}

// WHAT CLOSING A MODAL MEANS, AND IT IS THE ONLY PART OF CLOSING LEFT HERE.
//
// Every overlay in this app is a modal <dialog> (style.css, `.modal-overlay`), so
// the browser owns the closing itself: `closedby="any"` gives Escape and the
// backdrop click, a `command="close"` invoker gives the button, and focus goes
// back to whatever opened it. All three end in ONE `close` event, which is why a
// document-wide Escape listener, a document-wide backdrop listener and a close
// function callers had to remember to use are all gone.
//
// THE BUG THIS EXISTS FOR: the details modal embeds a trailer in an iframe, and
// closing it used to only remove a class — which hides the modal and leaves the
// iframe loaded, so a trailer went on playing, audible, over a calendar with
// nothing on screen to pause. It stopped only when another modal replaced the
// markup, or on a reload, or when the video reached its end.
//
// HERE RATHER THAN IN EACH CLOSER because there are three of them — the calendar,
// the share page and the tracker each open a details modal, all three build the
// same trailer block, and all three had the same fault.
//
// THE FRAME HAS TO GO, NOT JUST ITS `src`. Hiding an iframe does not stop it and
// nor does anything CSS can do to it — the document inside goes on running. The
// obvious next move, clearing the src, does not stop it either: checked in the
// browser, the attribute reads back as null while the trailer is still audible,
// because dropping the attribute does not tear down the document already loaded.
// Removing the ELEMENT does. The modal body is rebuilt from the payload every
// time one opens, so there is nothing here worth keeping.
//
// A `close` DOES NOT BUBBLE, so this listens in the CAPTURE phase rather than
// binding one listener per dialog — which is the same delegation every other
// primitive in this file uses, and it reaches an overlay the server delivered
// after the page loaded without anything having to re-arm.
document.addEventListener('close', (event) => {
    const modal = event.target;
    if (!(modal instanceof Element) || !modal.classList.contains('modal-overlay')) return;
    modal.querySelectorAll('iframe').forEach(frame => frame.remove());
    // Native media, for anything that grows one later: pausing is enough, and
    // unlike an iframe it keeps its position if it is shown again.
    modal.querySelectorAll('video, audio').forEach(media => {
        try { media.pause(); } catch (e) { /* a detached element cannot pause */ }
    });
    // AN OVERLAY THE SERVER DREW FOR ONE OPENING GOES WHEN IT CLOSES
    // (`data-modal-discard`: Settings, Share, Filters). The next opening fetches
    // it again, so there is nothing to keep — and removing it is what stops
    // anything it was doing: an htmx `every` poll clears itself only once its
    // element has left the page, and a closed dialog has not.
    if (modal.hasAttribute('data-modal-discard')) modal.remove();
}, true);

// AN OVERLAY THE SERVER DELIVERS ALREADY OPEN, PUT INTO THE TOP LAYER.
//
// THE ONE PART OF OPENING A DIALOG THAT MARKUP CANNOT STATE, and the reason is
// exact: the `open` ATTRIBUTE makes a NON-modal dialog — no top layer, no
// backdrop, no Escape — and there is no attribute for the modal kind. A button
// can say it (`command="show-modal"`), which is how every overlay the page
// carries is opened, but the Settings, Share, Filters and tier-settings panels
// are fragments fetched on demand and arrive with nobody pressing anything.
//
// `data-open-modal` IS THE SERVER SAYING "AND SHOW IT". Guarded on `open` so a
// fragment that is redelivered while it is already up — the tracker's rewatch
// question is answered with itself out of band — is not re-shown, which would
// throw.
document.addEventListener('htmx:after:swap', () => {
    document.querySelectorAll('dialog[data-open-modal]:not([open])')
        .forEach(modal => modal.showModal());
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
// NO GUARD ON THE TEMPLATE BEING THERE, deliberately. A page that offers the
// panel and has not included its shell is broken, and answering a missing
// template with an empty fragment made that look like a request that returned
// nothing — the one symptom this panel already has for two other causes.
// Throwing names the real fault. tests/kernel/test_page_head.py is what catches
// it before a browser has to.
function detailsPanelState(id) {
    return document.getElementById(id).content.cloneNode(true);
}

// WHAT THE OPENER IS CURRENTLY SHOWING, PUT ON THE ADDRESS IT OPENS.
//
// A calendar card can be showing either service's name, picture or description —
// it draws every one it was given and a radio group says which (_card.html) — and
// the panel has to open on the SAME one, or clicking a title you flipped to opens
// on the words you just replaced. The address the server built already names a
// service per field; a checked radio simply overrides the one it is about.
//
// `set` ON A PARSED ADDRESS rather than appending, because appending a second
// `title=` would leave the server reading the first. And it is general rather
// than the card's own: an opener that contains no such control changes nothing,
// which is every opener in the app but this one.
function detailsAddress(opener) {
    const address = new URL(opener.dataset.detailsOpen, window.location.href);
    opener.querySelectorAll('[data-details-field]:checked').forEach(
        pick => address.searchParams.set(pick.dataset.detailsField, pick.value));
    return address.pathname + address.search;
}

// THE HEADING IS SHOWN BEFORE THE ANSWER ARRIVES, so unlike the fields above it
// cannot travel on the address — it has to be the text itself. A control that
// carries one and is checked is the one being shown.
function detailsHeading(opener) {
    const titled = opener.querySelector('[data-details-heading]:checked');
    return (titled ? titled.dataset.detailsHeading : opener.dataset.detailsHeading) || 'Details';
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
    document.getElementById('detailsModal').showModal();
    htmx.ajax('GET', url, { target: '#detailsBody', swap: 'innerHTML' });
}

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
    openDetailsPanel(detailsAddress(opener), detailsHeading(opener));
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
//
// THE FIELD ANNOUNCES IT WAS FILLED, with a `file-read` event that bubbles, so a
// form that should post the moment a file is chosen says so in its own markup
// (`hx-trigger="file-read"`) — the account's profile picture is one. A form that
// waits for a typed confirmation simply does not listen. Its own event rather than
// `change`: the file input's `change` has already bubbled through the form by then,
// before the read finished, and a form listening for it would post an empty field.
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
    if (into) into.dispatchEvent(new Event('file-read', { bubbles: true }));
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

// `data-fill="<selector>"` with `data-fill-value` — write a value into a field.
//
// A BUTTON CANNOT SET ANOTHER ELEMENT'S VALUE, and there is no attribute that
// says it: the rewatch question's two presets are "count those episodes" and
// "do not", which are a person's words for two DAYS, and filling the one date
// field visibly is what shows a reader those are the same statement. Everything
// there answers into that field rather than around it, which is what stops a
// typed date being an option with no way to submit it.
//
// THE `input` EVENT IS THE POINT OF THE DISPATCH, not a formality. The field
// asks the server what the submit button should say (`hx-trigger="input
// changed"`, templates/_distrakt_rewatch.html), so a preset that set the value
// silently would leave the button naming the day before it.
//
// NAMING THE DAY IS NOT HERE ANY MORE. This used to write the button's text from
// a label the preset carried, and a second listener re-spelled it with
// toLocaleDateString as somebody typed — which disagreed with the three days the
// server had already spelled on the same dialog. One formatter answers for all
// four now (distrakt/routes.py's `_run_label`).
function fillField(field, value) {
    if (!field) return;
    field.value = value;
    field.dispatchEvent(new Event('input', { bubbles: true }));
    field.focus();
}

document.addEventListener('click', (e) => {
    if (!(e.target instanceof Element)) return;
    const preset = e.target.closest('[data-fill]');
    if (preset) fillField(document.querySelector(preset.dataset.fill),
                          preset.dataset.fillValue || '');
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
