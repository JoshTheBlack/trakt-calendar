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

/* The changelog modal, shared by every page carrying the header.

   Plain fetch rather than htmx, even though every page now ships htmx: the
   response goes into a modal this page already owns, which is a fetch, not a
   navigation. Fetched once and left in the DOM — the changelog cannot change
   under a running server, since a new one only arrives with a new container. */
let changelogLoaded = false;

async function openChangelog() {
    const modal = document.getElementById('changelogModal');
    if (!modal) return;
    modal.classList.add('open');
    if (changelogLoaded) return;
    const body = document.getElementById('changelogBody');
    try {
        const resp = await fetch('/api/changelog', { headers: { 'Accept': 'text/html' } });
        if (!resp.ok) throw new Error(resp.status);
        // Server-rendered first-party markup from CHANGELOG.md, with raw HTML
        // dropped at render time (see app/changelog.py). Nothing here is user
        // input, which is what makes innerHTML the right tool rather than a
        // hazard — cf. confirmInline() in ui.js, where the message IS untrusted.
        body.innerHTML = await resp.text();
        changelogLoaded = true;
    } catch (e) {
        body.innerHTML = '';
        const p = document.createElement('p');
        p.className = 'hint';
        p.textContent = "Couldn't load the changelog.";
        body.appendChild(p);
    }
}

function closeChangelog() {
    const modal = document.getElementById('changelogModal');
    if (modal) modal.classList.remove('open');
}

document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeChangelog();
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
