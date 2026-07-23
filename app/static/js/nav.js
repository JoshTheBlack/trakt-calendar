/* The site-wide header behaviour, shared by the calendar, the month picker, and
   the tracker so all three bars behave identically.

   The menu itself is a <details>: it opens, closes on Escape, and is keyboard
   reachable with no script at all. The only thing <details> does not do is close
   when you click somewhere else on the page, which is what this adds. */

function closeNavMenus() {
    document.querySelectorAll('details.nav-menu[open]').forEach(d => d.removeAttribute('open'));
}

document.addEventListener('click', (e) => {
    if (!e.target.closest('details.nav-menu')) closeNavMenus();
});

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

async function signOut() {
    try {
        const resp = await fetch('/logout', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
        });
        const data = await resp.json().catch(() => ({}));
        window.location = data.redirect || '/login';
    } catch (e) {
        // The session cookie is the server's to clear, so a failed request means
        // we may still be signed in — send them to the sign-in page either way.
        window.location = '/login';
    }
}
