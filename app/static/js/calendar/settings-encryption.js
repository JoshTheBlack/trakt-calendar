// ---- At-rest encryption panel (admin, Server tab) ----
// The key never touches this app beyond being revealed once for the operator to
// copy into their own environment; every transition is a call to
// /api/admin/encryption, and the panel just reflects the state it reports back.
function encAddButton(row, label, handler, ghost) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn-ghost small' + (ghost ? '' : ' btn-primary-inline');
    btn.textContent = label;
    btn.addEventListener('click', handler);
    row.appendChild(btn);
}

// The panel's own heading, not just the status line beneath it: "Secret
// encryption at rest" read as true whether or not anything was actually
// encrypted, which is exactly backwards for a security control. This makes
// the plaintext case impossible to mistake for the encrypted one at a glance.
function renderEncryptionHeading(s) {
    const heading = document.getElementById('s_enc_heading');
    if (s.phase === 'encrypted' && s.health !== 'key_missing' && !s.needs_reseal) {
        heading.textContent = '🔒 Secrets encrypted at rest';
        heading.className = 'settings-section enc-encrypted';
    } else if (s.health === 'key_missing') {
        heading.textContent = '⚠️ Secrets encrypted, key missing';
        heading.className = 'settings-section enc-plain';
    } else if (s.phase === 'encrypted' && s.needs_reseal) {
        // `phase` is a one-way ratchet — once a backfill has run it stays
        // "encrypted" forever, even if something written afterward (e.g. a
        // relink that landed while the key was unavailable) is still plaintext.
        // Read the actual rows instead of trusting the flag at face value.
        heading.textContent = '⚠️ Some secrets are unencrypted';
        heading.className = 'settings-section enc-plain';
    } else {
        heading.textContent = '⚠️ Secrets are stored in plaintext';
        heading.className = 'settings-section enc-plain';
    }
}

function renderEncryption(s) {
    const status = document.getElementById('s_enc_status');
    const actions = document.getElementById('s_enc_actions');
    const reveal = document.getElementById('s_enc_reveal');
    const err = document.getElementById('s_enc_error');
    renderEncryptionHeading(s);
    actions.innerHTML = '';
    reveal.hidden = true; reveal.innerHTML = '';
    err.hidden = true; err.textContent = '';
    const env = s.env_var || 'ENCRYPTION_KEY';

    if (s.health === 'key_missing') {
        status.innerHTML = '⚠️ Secrets are encrypted, but <code>' + esc(env) + '</code> is not set — ' +
            'they are unreadable but intact. Restore the key and restart. Do <strong>not</strong> ' +
            're-save credentials or re-link while it is missing: that overwrites the encrypted values.';
        // A plain link here would be easy to miss — bare <a> tags render as
        // unstyled text (see the global `a { text-decoration: none }` reset),
        // so this is a real button, not prose with a link buried in it.
        encAddButton(actions, 'Lost the key for good? Recover here →',
            () => { window.location.href = '/admin/encryption/recovery'; });
        return;
    }
    if (s.phase === 'encrypted') {
        if (s.needs_reseal) {
            status.innerHTML = 'Everything was sealed once, but at least one secret or linked ' +
                'token was written since — most likely while <code>' + esc(env) + '</code> was ' +
                'briefly unavailable — and is stored in the clear right now. Encrypt again to seal ' +
                'just that value; anything already sealed is left untouched.';
            encAddButton(actions, 'Encrypt secrets now', encEncryptNow);
            return;
        }
        status.textContent = '🔒 Stored secrets are encrypted at rest.';
        return;
    }
    if (s.key_valid) {
        if (s.phase === 'opted_out') {
            // Already declined once with this key present. Repeating the exact
            // same "Encrypt secrets now / Not now" offer here is why "Not now"
            // looked like a no-op — the panel showed the identical prompt either
            // way, so nothing visibly changed. This is the resting state
            // instead: a single action, no repeat decision to make.
            status.innerHTML = 'Not encrypted. A valid key is set in <code>' + esc(env) + '</code> — ' +
                'turn encryption on whenever you’re ready.';
            encAddButton(actions, 'Encrypt secrets now', encEncryptNow);
            return;
        }
        status.innerHTML = 'A valid key is set in <code>' + esc(env) + '</code>. Encrypt the stored ' +
            'secrets now — no restart needed.';
        encAddButton(actions, 'Encrypt secrets now', encEncryptNow);
        if (s.phase !== 'pending_encrypt') encAddButton(actions, 'Not now', encOptOut, true);
        return;
    }
    if (s.phase === 'pending_key' || s.phase === 'pending_encrypt') {
        status.innerHTML = 'Waiting for the key. Set <code>' + esc(env) + '</code> in your environment ' +
            '(env, Docker, or compose) and restart the app, then check again.';
        encAddButton(actions, '↻ Check for the key', encVerify);
        encAddButton(actions, 'Not now', encOptOut, true);
        return;
    }
    // phase 'none' or 'opted_out' with no key yet — the first offer.
    status.innerHTML = 'Encrypt the stored credentials and linked tokens so a leaked database ' +
        'file (a backup or snapshot) does not hand them over. The key lives in your environment, ' +
        'never in the database. <strong>You must save the key and never lose it</strong> — losing it ' +
        'means re-linking Trakt and re-entering every API key.';
    encAddButton(actions, 'Generate a key for me', () => encEnable(true));
    encAddButton(actions, 'I’ll set my own key', () => encEnable(false));
    if (s.phase === 'none') encAddButton(actions, 'Not now', encOptOut, true);
}

async function loadEncryptionState() {
    const panel = document.getElementById('s_enc_panel');
    if (!panel) return;  // non-admin page has no encryption panel
    try {
        const res = await fetch('/api/admin/encryption', { cache: 'no-store' });
        const s = await res.json();
        if (!s.ok) { document.getElementById('s_enc_status').textContent = 'Unavailable.'; return; }
        renderEncryption(s);
    } catch (e) { console.error(e); }
}

function encError(message) {
    const err = document.getElementById('s_enc_error');
    err.textContent = message || 'Something went wrong.';
    err.hidden = false;
}

async function encPost(path, body) {
    const res = await fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {}),
    });
    const data = await res.json().catch(() => ({}));
    return { ok: res.ok && data.ok, data };
}

async function encEnable(generate) {
    const { ok, data } = await encPost('/api/admin/encryption/enable', { generate });
    if (!ok) { encError(data.error); return; }
    if (data.restart_required) { encShowReveal(data.key, generate); return; }
    loadEncryptionState();  // a valid key was already present — straight to "encrypt now"
}

function encShowReveal(key, generated) {
    const env = 'ENCRYPTION_KEY';
    const reveal = document.getElementById('s_enc_reveal');
    document.getElementById('s_enc_actions').innerHTML = '';
    let inner = '';
    if (generated) {
        inner += '<p class="hint"><strong>Save this key now — it is shown once and never again.</strong> ' +
            'If you lose it, the encrypted secrets are unrecoverable.</p>' +
            '<div class="share-link-box"><input type="text" id="s_enc_key" readonly value="' + esc(key) + '">' +
            '<button type="button" class="btn-ghost small" onclick="encCopyKey()">Copy</button></div>';
    } else {
        inner += '<p class="hint">Generate a key with:</p>' +
            '<div class="share-link-box"><input type="text" readonly value="python -c &quot;from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())&quot;"></div>';
    }
    inner += '<p class="hint">Set it as <code>' + env + '</code> in your environment (env file, ' +
        'Docker <code>-e</code>, or compose <code>environment:</code>) and restart the app. Then:</p>';
    reveal.innerHTML = inner;
    encAddButton(document.getElementById('s_enc_actions'), '↻ I’ve set the key and restarted — check', encVerify);
    encAddButton(document.getElementById('s_enc_actions'), 'Not now', encOptOut, true);
    reveal.hidden = false;
}

function encCopyKey() {
    const input = document.getElementById('s_enc_key');
    if (!input) return;
    input.select();
    try { navigator.clipboard.writeText(input.value); } catch (_) { document.execCommand('copy'); }
    toast('Key copied — store it somewhere safe', true);
}

async function encVerify(event) {
    const { ok, data } = await encPost('/api/admin/encryption/verify', {});
    if (!ok) { encError(data.error); return; }
    if (!data.detected) {
        encError('No valid key found in the environment yet. Set ENCRYPTION_KEY and restart, then try again.');
        return;
    }
    encEncryptNow(event);
}

async function encEncryptNow(event) {
    confirmInline(event.currentTarget,
        'Encrypt every stored secret and linked token now? Make sure you have saved the key first.',
        async () => {
            const { ok, data } = await encPost('/api/admin/encryption/encrypt', {});
            if (!ok) { encError(data.error); return; }
            toast('Secrets encrypted at rest', true);
            loadEncryptionState();
        }, { danger: true });
}

async function encOptOut() {
    const { ok, data } = await encPost('/api/admin/encryption/opt-out', {});
    if (!ok) { encError(data.error); return; }
    loadEncryptionState();
}
