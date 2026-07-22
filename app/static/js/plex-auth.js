/* Log in with Plex — the popup + poll flow shared by the sign-in, register,
   and account pages.

   Plex has no redirect callback: the popup approves the PIN entirely on
   plex.tv's own page, and this app only ever finds out by asking. `plexAuth()`
   opens that popup and polls /auth/plex/poll every couple of seconds until the
   server reports the PIN approved (or the flow fails), then resolves with the
   {ok, redirect} body the poll endpoint returns on success. */

async function plexAuth(opts) {
  opts = opts || {};
  const startUrl = opts.link
    ? '/auth/plex/link'
    : ('/auth/plex/start' + (opts.invite ? ('?invite=' + encodeURIComponent(opts.invite)) : ''));
  const startResp = await fetch(startUrl, { headers: { Accept: 'application/json' } });
  const startData = await startResp.json().catch(() => ({}));
  if (!startResp.ok || !startData.ok) {
    throw new Error(startData.error || 'Could not start Plex sign-in.');
  }

  const popup = window.open(startData.popup_url, 'plex-auth', 'width=480,height=700');
  const state = startData.state;

  return new Promise((resolve, reject) => {
    let timer = null;
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
    }, 1500);
  });
}
