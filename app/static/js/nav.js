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
