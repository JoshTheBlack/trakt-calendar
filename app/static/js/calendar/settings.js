// The Settings modal (administrators only): the one form behind /api/settings,
// its write-only credential fields, and the hints that warn about a value before
// it is saved.
//
// Two panels inside this modal have their own files, because each answers to a
// different endpoint and changes for its own reasons: the at-rest encryption
// panel is calendar/settings-encryption.js and the Trakt device-code
// authorization is calendar/settings-trakt.js.

// The cache size cap is stored in bytes and edited in megabytes.
const MB = 1024 * 1024;
const GB = 1024 * MB;

// Credentials are write-only: the server sends back a flag per secret saying
// whether one is stored, never the value. So each credential input renders
// EMPTY, with a placeholder saying whether something is saved, and an empty
// input on save means "leave it as it is". Clearing one is a deliberate act —
// the ✕ next to the field — because otherwise every save would wipe them all.
function applySecretState(secretsSet) {
    document.querySelectorAll('input[data-secret]').forEach(input => {
        const stored = !!(secretsSet || {})[input.name];
        input.value = '';
        input.dataset.stored = stored ? '1' : '';
        input.dataset.clear = '';
        input.placeholder = stored ? 'Saved — leave blank to keep it' : 'Not set';
        const button = input.parentElement && input.parentElement.querySelector('[data-role="clear-secret"]');
        if (button) button.hidden = !stored;
        setSecretHint(input);
    });
}

function setSecretHint(input) {
    const row = input.parentElement;
    const hint = row && row.querySelector('[data-role="secret-hint"]');
    if (!hint) return;
    hint.textContent = input.dataset.clear ? 'Will be cleared when you save.' : '';
}

function clearSecret(button) {
    const input = button.parentElement.querySelector('input[data-secret]');
    if (!input) return;
    input.value = '';
    input.dataset.clear = '1';
    input.placeholder = 'Will be cleared';
    setSecretHint(input);
}

// Each credential input gets a ✕ beside it, built here rather than repeated six
// times in the template.
function buildSecretControls() {
    document.querySelectorAll('input[data-secret]').forEach(input => {
        const row = input.parentElement;
        if (!row || row.querySelector('[data-role="clear-secret"]')) return;
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn-ghost small';
        button.dataset.role = 'clear-secret';
        button.textContent = '✕ Clear';
        button.title = 'Remove the saved value when you save';
        button.hidden = true;
        button.addEventListener('click', () => clearSecret(button));
        const hint = document.createElement('span');
        hint.className = 'hint';
        hint.dataset.role = 'secret-hint';
        input.insertAdjacentElement('afterend', hint);
        input.insertAdjacentElement('afterend', button);
    });
}

// A secret goes into the payload only when the admin typed a new one or asked
// for it to be cleared. Anything else is omitted, which is what tells the server
// to leave the stored value alone.
function collectSecrets() {
    const payload = {};
    document.querySelectorAll('input[data-secret]').forEach(input => {
        const typed = input.value.trim();
        if (typed) payload[input.name] = typed;
        else if (input.dataset.clear) payload[input.name] = null;
    });
    return payload;
}

// The tabs are presentation only — every panel stays in the one <form>, and
// hidden inputs are read by id at save time, so switching tabs never drops a
// pending edit and one Save still writes all four groups.
function showSettingsTab(name) {
    document.querySelectorAll('#settingsModal [data-tab]').forEach(btn => {
        const on = btn.dataset.tab === name;
        btn.classList.toggle('active', on);
        btn.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    document.querySelectorAll('#settingsModal [data-tab-panel]').forEach(panel => {
        panel.hidden = panel.dataset.tabPanel !== name;
    });
    // The OVERLAY is the scroll container (the modal sits at flex-start inside
    // it), so a tall panel scrolled halfway down would leave the next tab
    // opening mid-content.
    const overlay = document.getElementById('settingsModal');
    if (overlay) overlay.scrollTop = 0;
}

async function openSettings() {
    showSettingsTab('server');
    buildSecretControls();
    try {
        const res = await fetch('/api/settings', { cache: 'no-store' });
        const s = await res.json();
        document.getElementById('s_base_url').value = s.public_base_url || '';
        updateBaseUrlHint(s);
        document.getElementById('s_trusted_proxies').value = s.trusted_proxy_ips || '';
        updateProxyHint(s);
        document.getElementById('s_cookie_secure').value = (s.cookie_secure || 'always').toLowerCase();
        updateCookieHint(s);
        // A <select> of two strings rather than a checkbox, so the two states
        // both say what they mean — "off" for a security setting is exactly the
        // kind of thing worth spelling out rather than leaving to a cleared box.
        document.getElementById('s_open_registration').value =
            s.allow_open_registration ? 'true' : 'false';
        document.getElementById('s_auto_approve').value =
            s.auto_approve_calendar ? 'true' : 'false';
        updateRegistrationHint();
        document.getElementById('s_client_id').value = s.trakt_client_id || '';
        applySecretState(s.secrets_set);
        updateTokenStatus(s.trakt_token_expires_at);
        updateTraktLoginHints(s);
        document.getElementById('s_timezone').value = s.timezone || '';
        document.getElementById('s_endpoint').value = s.endpoint || 'shows/new';
        document.getElementById('s_limit').value = s.pagination_limit || 300;
        document.getElementById('s_cache').value = (s.cache_ttl_minutes ?? 720);
        document.getElementById('s_calcache').value = (s.calendar_cache_ttl_minutes ?? 10);
        // Stored in bytes; shown in MB, because nobody wants to count zeros.
        document.getElementById('s_cachecap').value = Math.round((s.api_cache_max_bytes ?? 1073741824) / MB);
        // Shown in GB: the sensible values here are far larger than the API cache's.
        document.getElementById('s_postercap').value = Math.round((s.poster_cache_max_bytes ?? 10737418240) / GB);
        document.getElementById('s_hide').checked = !!s.hide_not_watching;
        document.getElementById('s_prewarm').checked = !!s.calendar_prewarm_enabled;
        document.getElementById('s_genres').value = s.genres || '';
        document.getElementById('s_countries').value = s.countries || '';
        setCertPicker(document.getElementById('s_show_certifications'), s.show_certifications || '');
        setCertPicker(document.getElementById('s_movie_certifications'), s.movie_certifications || '');
        document.getElementById('s_networks').value = (s.network_filter || []).join(', ');
        // Sonarr / Radarr
        document.getElementById('s_sonarr_url').value = s.sonarr_url || '';
        ensureOption(document.getElementById('s_sonarr_qp'), s.sonarr_quality_profile_id, 'Profile #' + s.sonarr_quality_profile_id);
        ensureOption(document.getElementById('s_sonarr_rf'), s.sonarr_root_folder, s.sonarr_root_folder);
        document.getElementById('s_radarr_url').value = s.radarr_url || '';
        ensureOption(document.getElementById('s_radarr_qp'), s.radarr_quality_profile_id, 'Profile #' + s.radarr_quality_profile_id);
        ensureOption(document.getElementById('s_radarr_rf'), s.radarr_root_folder, s.radarr_root_folder);
        document.getElementById('s_seer_url').value = s.seer_url || '';
    } catch (e) { console.error(e); }
    loadEncryptionState();
    document.getElementById('settingsModal').classList.add('open');
}

// Keep a saved <select> value selectable even before options are loaded from Sonarr/Radarr.
function ensureOption(sel, value, label) {
    if (!value) return;
    if (![...sel.options].some(o => o.value === String(value))) {
        const o = document.createElement('option');
        o.value = value; o.textContent = label || value;
        sel.appendChild(o);
    }
    sel.value = String(value);
}

async function loadArrOptions(kind) {
    const url = document.getElementById('s_' + kind + '_url').value.trim();
    const keyInput = document.getElementById('s_' + kind + '_key');
    const key = keyInput.value.trim();
    // A blank key with one already saved is the normal case now that the field
    // can't be read back — the server falls back to the stored one.
    if (!url || !(key || keyInput.dataset.stored)) {
        toast('Enter the ' + kind + ' URL and API key first', false);
        return;
    }
    try {
        const res = await fetch('/api/integrations/options', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ kind, url, api_key: key })
        });
        const d = await res.json();
        if (!d.ok) { toast(d.error || ('Could not load ' + kind + ' options'), false); return; }
        const qp = document.getElementById('s_' + kind + '_qp');
        const rf = document.getElementById('s_' + kind + '_rf');
        const savedQp = qp.value, savedRf = rf.value;
        qp.innerHTML = (d.profiles || []).map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join('');
        rf.innerHTML = (d.folders || []).map(f => `<option value="${esc(f.path)}">${esc(f.path)}</option>`).join('');
        if (savedQp) qp.value = savedQp;
        if (savedRf) rf.value = savedRf;
        toast(kind.charAt(0).toUpperCase() + kind.slice(1) + ' options loaded', true);
    } catch (e) { toast('Could not load ' + kind + ' options', false); }
}

function closeSettings() {
    stopDeviceAuthPolling();
    document.getElementById('settingsModal').classList.remove('open');
}

// The PUBLIC_BASE_URL environment variable WINS over the saved base URL, because
// the lockout it exists to rescue is a saved value that no longer matches where
// the operator is browsing — a wrong one refuses every mutating request, this
// screen's own save included. Winning silently would just move the trap: the
// field would accept a correction that never took effect. So while the variable
// is set, the field says so in place, in the same warn style as the cookie and
// proxy hints. The field itself shows the value in force, so saving it is how
// the operator makes the rescue permanent before dropping the variable.
function updateBaseUrlHint(s) {
    const hint = document.getElementById('s_base_url_env');
    if (!hint) return;
    hint.hidden = !s.public_base_url_overridden;
    hint.textContent = s.public_base_url_overridden
        ? 'The PUBLIC_BASE_URL environment variable is set and it overrides this field. '
        + 'Save the value you want here, then remove the variable and restart — until '
        + 'you do, anything saved here has no effect.'
        : '';
}

// Tells the operator what to type instead of making them work out their own
// container network, and calls out the one combination that fails silently:
// forwarded headers arriving from a peer this app doesn't trust, which collapses
// every user onto the proxy's address for rate limiting and the session list.
function updateProxyHint(s) {
    const hint = document.getElementById('s_proxy_hint');
    if (!hint) return;
    const peer = s.detected_peer_ip || 'unknown';
    let text = 'Comma-separated CIDRs whose X-Forwarded-For this app will honor. '
        + 'This request came from ' + peer + '.';
    if (s.forwarded_headers_present && !s.peer_is_trusted_proxy) {
        text += ' Forwarded headers ARE arriving but are being ignored, so every user '
            + 'currently looks like ' + peer + ' — add it here.';
        hint.classList.add('warn');
    } else {
        hint.classList.remove('warn');
    }
    hint.textContent = text;
}

// The stored policy can drift from reality — a data dir that started on
// localhost and later moved behind HTTPS keeps "never" and quietly serves
// session cookies with no Secure flag. Only the browser knows the real protocol,
// so this compares the saved value against it and warns in place, over the
// field's own explanatory text. `s` may be the settings response OR nothing (a
// re-check after the dropdown changes), so the mode is read from the control
// when not passed.
function updateCookieHint(s) {
    const select = document.getElementById('s_cookie_secure');
    const hint = document.getElementById('s_cookie_hint');
    if (!select || !hint) return;
    const mode = ((s && s.cookie_secure) || select.value || 'always').toLowerCase();
    const isHttps = window.location.protocol === 'https:';
    let text = '';
    if (mode === 'never' && isHttps) {
        text = 'You reached this page over https:// but cookies are set to "never" — '
            + 'they are going out without the Secure flag. Choose "Always".';
    } else if (mode === 'always' && !isHttps) {
        text = 'You reached this page over http://. Saving "Always" would make the session '
            + 'cookie Secure and your browser would discard it, locking you out — this is '
            + 'refused. Serve over https:// (a reverse proxy counts), or choose Auto or Never.';
    }
    hint.textContent = text || hint.dataset.base || '';
    hint.classList.toggle('warn', !!text);
}

// The same in-place warning pattern as updateCookieHint, for the two settings
// that decide who can get in. Both are read together because it is the PAIR that
// matters: either alone is ordinary, while open registration plus automatic
// approval means anyone who can reach the instance gets working calendar access
// with nobody in the loop. That is worth saying at the moment the choice is
// made, not after the save.
function updateRegistrationHint() {
    const openSelect = document.getElementById('s_open_registration');
    const autoSelect = document.getElementById('s_auto_approve');
    const openHint = document.getElementById('s_registration_hint');
    const autoHint = document.getElementById('s_auto_approve_hint');
    if (!openSelect || !autoSelect || !openHint || !autoHint) return;
    const open = openSelect.value === 'true';
    const auto = autoSelect.value === 'true';

    let openText = '';
    if (open && auto) {
        openText = 'Anyone who can reach this instance can create an account AND will have '
            + 'calendar access immediately, with no administrator involved. Only do this on '
            + 'an instance you are happy to have open.';
    } else if (open) {
        openText = 'Anyone who can reach this instance will be able to create an account '
            + 'without an invite. They arrive unapproved and can see nothing until an '
            + 'administrator approves them.';
    }
    openHint.textContent = openText || openHint.dataset.base || '';
    openHint.classList.toggle('warn', !!openText);

    // Flagged on this field only when registration is open; with invites still
    // required, approving on arrival is a normal convenience — somebody was
    // already trusted enough to be handed a link.
    const autoText = (auto && open)
        ? 'Combined with open registration, this hands calendar access to anyone who signs up.'
        : '';
    autoHint.textContent = autoText || autoHint.dataset.base || '';
    autoHint.classList.toggle('warn', !!autoText);
}

async function saveSettings(event) {
    event.preventDefault();
    const payload = {
        ...collectSecrets(),
        public_base_url: document.getElementById('s_base_url').value.trim(),
        trusted_proxy_ips: document.getElementById('s_trusted_proxies').value.trim(),
        cookie_secure: document.getElementById('s_cookie_secure').value,
        // A real boolean, not the select's string: the server coerces either way
        // (config._as_bool), but sending the wrong type and relying on that is
        // how "false" ends up truthy somewhere later.
        allow_open_registration: document.getElementById('s_open_registration').value === 'true',
        auto_approve_calendar: document.getElementById('s_auto_approve').value === 'true',
        trakt_client_id: document.getElementById('s_client_id').value.trim(),
        timezone: document.getElementById('s_timezone').value.trim() || 'Europe/Athens',
        endpoint: document.getElementById('s_endpoint').value,
        pagination_limit: parseInt(document.getElementById('s_limit').value, 10) || 300,
        cache_ttl_minutes: parseInt(document.getElementById('s_cache').value, 10) || 0,
        calendar_cache_ttl_minutes: parseInt(document.getElementById('s_calcache').value, 10) || 10,
        api_cache_max_bytes: (parseInt(document.getElementById('s_cachecap').value, 10) || 1024) * MB,
        poster_cache_max_bytes: (parseInt(document.getElementById('s_postercap').value, 10) || 10) * GB,
        hide_not_watching: document.getElementById('s_hide').checked,
        calendar_prewarm_enabled: document.getElementById('s_prewarm').checked,
        genres: document.getElementById('s_genres').value,
        countries: document.getElementById('s_countries').value,
        show_certifications: readCertPicker(document.getElementById('s_show_certifications')),
        movie_certifications: readCertPicker(document.getElementById('s_movie_certifications')),
        network_filter: document.getElementById('s_networks').value,
        sonarr_url: document.getElementById('s_sonarr_url').value.trim(),
        sonarr_quality_profile_id: parseInt(document.getElementById('s_sonarr_qp').value, 10) || 0,
        sonarr_root_folder: document.getElementById('s_sonarr_rf').value,
        radarr_url: document.getElementById('s_radarr_url').value.trim(),
        radarr_quality_profile_id: parseInt(document.getElementById('s_radarr_qp').value, 10) || 0,
        radarr_root_folder: document.getElementById('s_radarr_rf').value,
        seer_url: document.getElementById('s_seer_url').value.trim()
    };
    try {
        const res = await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const d = await res.json().catch(() => ({}));
        if (!res.ok || !d.ok) {
            // The server validates the public base URL, so its message says what
            // is wrong with the value rather than just that the save failed.
            toast(d.error || 'Could not save settings', false);
            return false;
        }
        window.location.reload();
    } catch (e) {
        console.error(e);
        alert('⚠️ Could not save settings.');
    }
    return false;
}
