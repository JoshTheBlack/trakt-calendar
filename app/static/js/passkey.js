/* Passkeys: THE BROWSER'S CREDENTIAL API, and nothing else.
 *
 * No markup, stylesheet rule, template or route can call navigator.credentials:
 * a passkey is made and used on the device, its private key never leaves it, and
 * only script in the page can ask the device for it. Everything around that is
 * the server's — the options ride on the button (a handshake minted before the
 * press, so the press is the gesture the prompt needs), and every sentence,
 * refusal and success is drawn by app/auth/routes.py. What this file does:
 *   - on a press of [data-passkey-get] / [data-passkey-create], asks the device,
 *     writes the answer (or the browser's error's NAME) into the control's form
 *     and submits it, through htmx's json-enc;
 *   - reveals a control drawn `hidden` with data-passkey-origin only where this
 *     page IS that origin and the browser has the API — which address the page
 *     was reached by is something the server is deliberately not told;
 *   - starts the username box's autofill request (`mediation: "conditional"`)
 *     with the button's options, and aborts it when the button is pressed: one
 *     challenge, and whichever finishes first spends it.
 * The JSON helpers are the browser's own where it has them, a small base64url
 * fallback where it has not.
 */
const passkeyAutofill = { button: null, controller: null };

const passkeyBytes = (text) => Uint8Array.from(
    atob(text.replace(/-/g, '+').replace(/_/g, '/')), (c) => c.charCodeAt(0)).buffer;
const passkeyText = (buffer) => btoa(String.fromCharCode(...new Uint8Array(buffer)))
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
const passkeyIds = (list) => (list || []).map((c) => ({ ...c, id: passkeyBytes(c.id) }));

function passkeyOptions(button) {
    const json = JSON.parse(button.dataset.passkeyGet || button.dataset.passkeyCreate);
    if (button.dataset.passkeyGet) {
        return PublicKeyCredential.parseRequestOptionsFromJSON?.(json)
            ?? { ...json, challenge: passkeyBytes(json.challenge), allowCredentials: passkeyIds(json.allowCredentials) };
    }
    return PublicKeyCredential.parseCreationOptionsFromJSON?.(json)
        ?? { ...json, challenge: passkeyBytes(json.challenge), user: { ...json.user, id: passkeyBytes(json.user.id) },
             excludeCredentials: passkeyIds(json.excludeCredentials) };
}

function passkeyJSON(credential) {
    if (credential.toJSON) return credential.toJSON();
    const r = credential.response;
    const response = { clientDataJSON: passkeyText(r.clientDataJSON) };
    if (r.attestationObject) {
        Object.assign(response, { attestationObject: passkeyText(r.attestationObject),
                                  transports: r.getTransports?.() ?? [] });
    } else {
        Object.assign(response, { authenticatorData: passkeyText(r.authenticatorData), signature: passkeyText(r.signature),
                                  userHandle: r.userHandle ? passkeyText(r.userHandle) : null });
    }
    return { id: credential.id, rawId: passkeyText(credential.rawId), type: credential.type, response,
             clientExtensionResults: credential.getClientExtensionResults() };
}

function passkeySend(form, credential, error) {
    form.elements.credential.value = credential ? JSON.stringify(passkeyJSON(credential)) : '';
    form.elements.error.value = error;
    form.requestSubmit();
}

document.addEventListener('click', async (e) => {
    const button = e.target instanceof Element && e.target.closest('[data-passkey-get],[data-passkey-create]');
    if (!button) return;
    passkeyAutofill.controller?.abort();
    const ask = button.dataset.passkeyGet ? 'get' : 'create';
    try {
        passkeySend(button.form, await navigator.credentials[ask]({ publicKey: passkeyOptions(button) }), '');
    } catch (err) {
        passkeySend(button.form, null, err.name || 'Error');
    }
});

function passkeyArrived() {
    if (!window.PublicKeyCredential) return;
    for (const control of document.querySelectorAll('[data-passkey-origin][hidden]')) {
        if (control.dataset.passkeyOrigin === location.origin) control.hidden = false;
    }
    const button = document.querySelector('[data-passkey-get]');
    if (!button || button.closest('[hidden]') || button === passkeyAutofill.button
        || !document.querySelector('input[autocomplete~="webauthn"]')) return;
    passkeyAutofill.controller?.abort();
    const controller = passkeyAutofill.controller = new AbortController();
    passkeyAutofill.button = button;
    (PublicKeyCredential.isConditionalMediationAvailable?.() ?? Promise.resolve(false)).then(async (available) => {
        if (!available || controller.signal.aborted) return;
        try {
            const credential = await navigator.credentials.get(
                { publicKey: passkeyOptions(button), mediation: 'conditional', signal: controller.signal });
            passkeySend(button.form, credential, '');
        } catch {
            // Aborted by a press of the button, or nothing was chosen: the button stands.
        }
    });
}

document.addEventListener('DOMContentLoaded', passkeyArrived);
document.addEventListener('htmx:after:swap', passkeyArrived);

// THE SIGN-IN PAGE'S BUTTON IS ASKED FOR FROM HERE (auth_login.html says why an
// htmx `load` trigger cannot), once htmx has wired the slot — now, for a slot
// already wired as this file runs, or when htmx announces it — and only where a
// passkey could be used at all.
function passkeyOffer(slot) {
    if (slot.dataset.passkeyAsked || !window.PublicKeyCredential
        || slot.dataset.passkeyOffer !== location.origin) return;
    slot.dataset.passkeyAsked = '1';
    htmx.trigger(slot, 'passkey-offer');
}
document.querySelectorAll('[data-passkey-offer][data-htmx-powered]').forEach(passkeyOffer);
document.addEventListener('htmx:after:init', (e) => {
    if (e.target instanceof Element && e.target.matches('[data-passkey-offer]')) passkeyOffer(e.target);
});
