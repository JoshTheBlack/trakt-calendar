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
