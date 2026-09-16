/* The site-wide header behaviour, shared by every page that carries the header.

   The menu itself is a <details>: it opens, closes on Escape, and is keyboard
   reachable with no script at all. The one thing <details> does not do is close
   when you click somewhere else on the page — or when you press one of its own
   entries that opens something rather than navigating (Settings, What's new) —
   which is what this adds. Everything those entries open is markup the server
   draws: the changelog is an `hx-get` into its overlay, Settings is a fragment,
   and sign-out is a button that posts itself and is answered with HX-Redirect.
   THIS FILE IS THE MENU'S LAST REMAINING BEHAVIOUR. */

function closeNavMenus() {
    document.querySelectorAll('details.nav-menu[open]').forEach(d => d.removeAttribute('open'));
}

document.addEventListener('click', (e) => {
    if (!e.target.closest('details.nav-menu') || e.target.closest('.nav-menu-items button')) {
        closeNavMenus();
    }
});
