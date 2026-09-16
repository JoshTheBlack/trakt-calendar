/* The site-wide header behaviour, shared by every page that carries the header.

   The menu itself is a <details>: it opens, closes on Escape, and is keyboard
   reachable with no script at all. The one thing <details> does not do is close
   when you click somewhere else on the page — or when you press one of its own
   entries that opens something rather than navigating (Settings, What's new) —
   which is what this adds. Everything those entries open is markup the server
   draws: the changelog is an `hx-get` into its overlay, and Settings is a
   fragment. */

function closeNavMenus() {
    document.querySelectorAll('details.nav-menu[open]').forEach(d => d.removeAttribute('open'));
}

document.addEventListener('click', (e) => {
    if (!e.target.closest('details.nav-menu') || e.target.closest('.nav-menu-items button')) {
        closeNavMenus();
    }
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
