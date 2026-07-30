// ---- Trakt OAuth device-code authorization ----
let deviceAuthTimer = null;

function updateTokenStatus(expiresAt) {
    const el = document.getElementById('s_token_status');
    if (!el) return;
    if (!expiresAt) { el.textContent = ''; return; }
    const when = new Date(expiresAt * 1000);
    const past = when.getTime() < Date.now();
    el.textContent = past
        ? `Token expired ${when.toLocaleDateString()} — refreshing automatically, or click "Refresh token now".`
        : `Token valid until ${when.toLocaleDateString()} (refreshes automatically once it expires).`;
}

// Shows the exact redirect URI to register on the Trakt application (it has to
// match byte for byte, so showing it beats describing it), and raises the
// reconnect prompt left behind when a saved token couldn't be matched to an
// account during first-run setup.
function updateTraktLoginHints(s) {
    const field = document.getElementById('s_redirect_field');
    const input = document.getElementById('s_redirect_uri');
    if (field && input) {
        input.value = s.trakt_redirect_uri || '';
        // Nothing to register until a public base URL exists to build it from.
        field.hidden = !s.trakt_redirect_uri;
    }
    const box = document.getElementById('s_reconnect_box');
    if (box) box.hidden = !s.trakt_reconnect_notice;
    showReconnectError('');
}

function showReconnectError(message) {
    const el = document.getElementById('s_reconnect_error');
    if (!el) return;
    el.textContent = message || '';
    el.classList.toggle('warn', !!message);
}

// Retry linking the token this instance already has, and report what stopped it.
// The notice's only previous escape was the OAuth flow, which cannot help when
// the blocker is that another login here already holds the Trakt account.
async function adoptTraktToken(btn) {
    btn.disabled = true;
    showReconnectError('');
    try {
        const res = await fetch('/api/auth/trakt/adopt', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}'
        });
        const d = await res.json();
        if (d.ok) {
            document.getElementById('s_reconnect_box').hidden = true;
            toast('Trakt account linked', true);
            return;
        }
        showReconnectError(d.error || 'Could not link the saved token.');
        toast('Could not link the saved token', false);
    } catch (e) {
        console.error(e);
        showReconnectError('Could not reach the server.');
    } finally {
        btn.disabled = false;
    }
}

function copyRedirectUri() {
    const input = document.getElementById('s_redirect_uri');
    if (!input || !input.value) return;
    navigator.clipboard.writeText(input.value).then(
        () => toast('Redirect URI copied', true),
        () => toast('Could not copy', false)
    );
}

function stopDeviceAuthPolling() {
    if (deviceAuthTimer) { clearInterval(deviceAuthTimer); deviceAuthTimer = null; }
}

// Show or hide the pairing panel, and lock the start button while a code is
// live. Re-clicking "Authorize with Trakt" used to request a FRESH code, so the
// one you had just copied stopped working — a trap, because the button looks
// exactly like the thing you press to open the page you paste the code into.
function setDeviceAuthActive(active) {
    const panel = document.getElementById('s_device_panel');
    const start = document.getElementById('s_device_start');
    if (panel) panel.hidden = !active;
    if (start) {
        start.disabled = active;
        start.textContent = active ? '⏳ Waiting for approval…' : '🔑 Authorize with Trakt';
    }
}

function copyDeviceCode() {
    const input = document.getElementById('s_device_code');
    if (!input || !input.value) return;
    navigator.clipboard.writeText(input.value).then(
        () => toast('Pairing code copied', true),
        () => toast('Could not copy — select it and copy by hand', false)
    );
}

function cancelDeviceAuth() {
    stopDeviceAuthPolling();
    setDeviceAuthActive(false);
    const box = document.getElementById('authStatus');
    if (box) box.textContent = 'Authorization cancelled.';
}

async function startDeviceAuth() {
    stopDeviceAuthPolling();
    const clientId = document.getElementById('s_client_id').value.trim();
    const secretInput = document.getElementById('s_client_secret');
    const clientSecret = secretInput.value.trim();
    const box = document.getElementById('authStatus');
    if (!clientId) { toast('Enter your Trakt Client ID first', false); return; }
    // Blank is fine when one is already saved; the poll endpoint falls back to it.
    if (!clientSecret && !secretInput.dataset.stored) {
        toast('Enter your Trakt Client Secret first', false);
        return;
    }
    box.textContent = 'Requesting a device code…';
    try {
        const res = await fetch('/api/auth/device/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ client_id: clientId })
        });
        const d = await res.json();
        if (!d.ok) { box.textContent = d.error || 'Could not start authorization.'; toast(d.error || 'Could not start authorization', false); return; }
        document.getElementById('s_device_code').value = d.user_code || '';
        const link = document.getElementById('s_device_link');
        link.href = d.verification_url || 'https://trakt.tv/activate';
        setDeviceAuthActive(true);
        box.textContent = 'Copy the code, open trakt.tv, and enter it there. Waiting for approval…';
        const deadline = Date.now() + (d.expires_in || 600) * 1000;
        const intervalMs = Math.max(d.interval || 5, 5) * 1000;
        deviceAuthTimer = setInterval(
            () => pollDeviceAuth(d.device_code, clientId, clientSecret, deadline),
            intervalMs
        );
    } catch (e) {
        console.error(e);
        box.textContent = 'Could not start authorization.';
    }
}

async function pollDeviceAuth(deviceCode, clientId, clientSecret, deadline) {
    const box = document.getElementById('authStatus');
    if (Date.now() > deadline) {
        stopDeviceAuthPolling();
        setDeviceAuthActive(false);
        box.textContent = 'The code expired before it was approved — try again.';
        return;
    }
    try {
        const res = await fetch('/api/auth/device/poll', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ device_code: deviceCode, client_id: clientId, client_secret: clientSecret })
        });
        const d = await res.json();
        if (d.status === 'pending' || d.status === 'slow_down') return;  // keep waiting
        stopDeviceAuthPolling();
        setDeviceAuthActive(false);
        if (d.status === 'authorized') {
            // The token isn't sent back — it is already saved server-side, and
            // putting a bearer token in page memory would serve no purpose.
            const tokenInput = document.getElementById('s_access_token');
            tokenInput.value = '';
            tokenInput.dataset.stored = '1';
            tokenInput.placeholder = 'Saved — leave blank to keep it';
            updateTokenStatus(d.expires_at);
            // The server also tries to adopt the fresh token as this admin's own
            // linked identity, which is what takes the reconnect notice down.
            // When that part fails it says why: an authorization that "worked"
            // yet left the notice up is the state that reads as the app ignoring
            // what you just did.
            if (d.trakt_linked) {
                const notice = document.getElementById('s_reconnect_box');
                if (notice) notice.hidden = true;
                box.textContent = '✅ Authorized, and linked to your account.';
            } else if (d.trakt_link_error) {
                box.textContent = '✅ Authorized, but not linked to your login: ' + d.trakt_link_error;
                showReconnectError(d.trakt_link_error);
            } else {
                box.textContent = '✅ Authorized! The access token has been saved.';
            }
            toast('Trakt authorized', true);
        } else {
            box.textContent = d.error || 'Authorization failed.';
            toast(d.error || 'Authorization failed', false);
        }
    } catch (e) {
        console.error(e);  // transient network hiccup; keep polling until the deadline
    }
}

async function refreshTraktToken() {
    const box = document.getElementById('authStatus');
    try {
        // Body-less, but still declared JSON: every mutating request in this app
        // has to be, or it is refused before it reaches the handler.
        const res = await fetch('/api/auth/refresh', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}'
        });
        const d = await res.json();
        if (!d.ok) { toast(d.error || 'Refresh failed', false); if (box) box.textContent = d.error || ''; return; }
        updateTokenStatus(d.expires_at);
        toast('Trakt token refreshed', true);
    } catch (e) {
        console.error(e);
        toast('Refresh failed', false);
    }
}
