/* Sign in with Plex: THE POPUP, and nothing else.
 *
 * Everything else in this flow is the server's. The start is the button's own
 * request, the waiting line and its fallback link are markup, the poll is a form
 * posting itself every couple of seconds, a refusal or a timeout is drawn where
 * that form was, and signing in is an HX-Redirect — see templates/_plex_flow.html
 * and app/auth/plex_routes.py. This file used to hold all of that: a fetch for the
 * start, an interval timer, a three-minute clock, the error sentences and a
 * fallback link built with createElement.
 *
 * WHY THE WINDOW CANNOT BE MARKUP. A browser opens a window only inside the click
 * that asked for one; opening it after the start request returns means the
 * gesture is spent and the window is blocked. And the address it has to show — a
 * PIN plex.tv issues on that request — does not exist yet at the moment of the
 * click. A link can open a window in the click, but only to an address it already
 * has. So the window is opened blank, here, in the click, and pointed at plex.tv
 * when the start's answer arrives. The same answer carries that address as a
 * link, which is the way round every failure of this file: a blocked window, one
 * behind this one, or one an extension stopped from loading.
 *
 * CAPTURE PHASE, so this runs before htmx's own listener on the button builds its
 * request: that request carries the field this writes, which is how the server
 * knows to say the window was blocked.
 */
let plexWindow = null;
let plexWindowAimed = false;

document.addEventListener('click', (e) => {
    if (!(e.target instanceof Element)) return;
    const button = e.target.closest('[data-plex-popup]');
    if (!button) return;
    plexWindow = window.open('', 'plex-auth', 'width=480,height=700');
    plexWindowAimed = false;
    const blocked = document.querySelector(button.dataset.plexPopup);
    if (blocked) blocked.value = plexWindow ? '' : 'blocked';
}, true);

// THE WINDOW LIVES EXACTLY AS LONG AS A WAITING BLOCK IS ON THE PAGE. The block
// carries the address; the first swap that brings one aims the window there, and
// the first swap that leaves none — a refusal, a timeout, a boosted navigation
// away — closes it. `replace` so the blank page is not a history entry to go Back
// into. A poll still waiting answers 204 and swaps nothing, so it never reaches
// this.
document.addEventListener('htmx:after:swap', () => {
    if (!plexWindow || plexWindow.closed) return;
    const link = document.querySelector('[data-plex-popup-url]');
    if (!link) {
        plexWindow.close();
        plexWindow = null;
        return;
    }
    if (!plexWindowAimed) {
        plexWindow.location.replace(link.href);
        plexWindowAimed = true;
    }
});

// Signing in is a real navigation, which no swap announces.
window.addEventListener('pagehide', () => {
    if (plexWindow && !plexWindow.closed) plexWindow.close();
});
