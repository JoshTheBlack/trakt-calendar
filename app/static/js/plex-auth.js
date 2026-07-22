/* Log in with Plex — the popup + poll flow shared by the sign-in, register,
   and account pages.

   Plex has no redirect callback: the popup approves the PIN entirely on
   plex.tv's own page, and this app only ever finds out by asking. `plexAuth()`
   opens that popup and polls /auth/plex/poll every couple of seconds until the
   server reports the PIN approved (or the flow fails), then resolves with the
   {ok, redirect} body the poll endpoint returns on success.

   THE WINDOW IS OPENED SYNCHRONOUSLY, before the PIN request, and navigated
   afterwards. A browser only allows window.open while it is still inside the
   click that caused it; opening it after `await fetch(...)` means the gesture
   has already been consumed, and the popup is then blocked or opened in a state
   that never loads. This is why plexAuth() must be CALLED synchronously from a
   click handler — do not await anything before it. */

// Long enough for someone to find the window, sign in to plex.tv, and approve;
// short enough that a popup which never loaded reports a real error instead of
// spinning until the tab is closed.
const PLEX_POLL_INTERVAL_MS = 1500;
const PLEX_TIMEOUT_MS = 3 * 60 * 1000;

function plexPopupBlocked() {
  return new Error(
    'Your browser blocked the Plex sign-in window. Allow pop-ups for this site and try again.'
  );
}

async function plexAuth(opts) {
  opts = opts || {};

  // FIRST, while the click is still live. About:blank now, real URL below.
  const popup = window.open('', 'plex-auth', 'width=480,height=700');
  if (!popup) throw plexPopupBlocked();

  let startData;
  try {
    const startUrl = opts.link
      ? '/auth/plex/link'
      : ('/auth/plex/start' + (opts.invite ? ('?invite=' + encodeURIComponent(opts.invite)) : ''));
    const startResp = await fetch(startUrl, { headers: { Accept: 'application/json' } });
    startData = await startResp.json().catch(() => ({}));
    if (!startResp.ok || !startData.ok) {
      throw new Error(startData.error || 'Could not start Plex sign-in.');
    }
  } catch (e) {
    popup.close();
    throw e;
  }

  // `replace` so the blank page we just opened doesn't become a history entry
  // the user can hit Back into.
  popup.location.replace(startData.popup_url);

  const state = startData.state;
  // Surfaced by the caller when the popup can't be seen or won't load — some
  // browsers open it behind the main window, and an extension or a network that
  // blocks app.plex.tv leaves it hanging with nothing to click.
  plexAuth.lastPopupUrl = startData.popup_url;

  return new Promise((resolve, reject) => {
    let timer = null;
    const startedAt = Date.now();
    const stop = () => {
      if (timer) clearInterval(timer);
      timer = null;
      if (popup && !popup.closed) popup.close();
    };
    timer = setInterval(async () => {
      if (popup && popup.closed) {
        stop();
        reject(new Error('The Plex sign-in window was closed.'));
        return;
      }
      if (Date.now() - startedAt > PLEX_TIMEOUT_MS) {
        stop();
        reject(new Error(
          "Timed out waiting for Plex. If the sign-in window never loaded, open it "
          + 'directly with the link below.'
        ));
        return;
      }
      try {
        const resp = await fetch('/auth/plex/poll', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ state: state }),
        });
        const data = await resp.json().catch(() => ({}));
        if (resp.ok && data.ok && data.status === 'pending') return;
        stop();
        if (resp.ok && data.ok) {
          resolve(data);
        } else {
          reject(new Error(data.error || 'Plex sign-in failed.'));
        }
      } catch (e) {
        stop();
        reject(e);
      }
    }, PLEX_POLL_INTERVAL_MS);
  });
}

/* Render "…or open the Plex window yourself" into an error line, once a flow has
   failed and there is a URL worth offering. Shared so the three pages that host
   this flow report the same fallback the same way. */
function plexFallbackLink(container) {
  if (!plexAuth.lastPopupUrl || !container) return;
  const a = document.createElement('a');
  a.href = plexAuth.lastPopupUrl;
  a.target = '_blank';
  a.rel = 'noopener';
  a.textContent = 'Open the Plex sign-in page in a new tab';
  container.appendChild(document.createElement('br'));
  container.appendChild(a);
}
