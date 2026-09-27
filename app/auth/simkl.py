"""Simkl OAuth v2 — the redirect authorization flow with PKCE, refresh, revoke,
and the account lookup that turns a token into an identity.

ONE FLOW, NOT TWO. Simkl documents an authorization-code redirect and nothing
else: there is no device-code path here the way there is for Trakt, so there is
no break-glass route for an operator whose registered redirect URI is wrong. The
only fix for that is to correct it on simkl.com, which is why the Settings screen
shows the exact URI to register rather than leaving the operator to build it.

  1. generate_pkce_pair() -> (verifier, challenge). The verifier is stored on the
     handshake row (server-side only — see app/auth/handshakes.py); the browser
     is redirected to authorize_url(..., code_challenge=challenge).
  2. Simkl redirects back to `redirect_uri` with ?code=&state=.
  3. POST /oauth2/token, form-encoded, {client_id, client_secret, code,
     redirect_uri, code_verifier, grant_type: "authorization_code"} ->
     {access_token, refresh_token, token_type, scope, expires_in}.

`redirect_uri` must match the value registered on the Simkl developer
application EXACTLY — Simkl compares it byte for byte, and again during the
exchange — which is why it is built from the configured public base URL and
never from a request header.

PKCE IS MANDATORY UNDER V2, S256 ONLY, FOR EVERY CLIENT TYPE INCLUDING SERVER
APPS (api-reference/oauth2-pkce) — it is not optional hardening layered on top
of the client secret, it is a precondition of the exchange succeeding at all.

TOKENS LAST ABOUT 7 DAYS AND COME WITH A REFRESH TOKEN: 180 days, SLIDING
(every refresh renews it) and NON-ROTATING (the same refresh token comes back
each time) — api-reference/oauth2-tokens. That is why this module now has
refresh_access_token where the V1 module had none, and why an identity minted
here carries a real `token_expires_at` instead of NULL.

ONE GRANT = ONE LIVE ACCESS TOKEN: refreshing replaces it immediately, so two
concurrent refreshes of the same grant cut each other off. That is why every
caller of refresh_access_token goes through the identity row's existing
refresh lease (auth.claim_identity_refresh / release_identity_refresh) — the
same one Trakt's refresh already serializes through, not a second mechanism.

SCOPE IS DECIDED BY REQUEST SHAPE (api-reference/oauth2-scopes): a GET with no
body needs `media:read`; anything else — a POST, a PUT, a DELETE, any request
carrying a body — needs `media:write`. This app asks for both from the first
link (`media:read media:write`), because the tracker's watchlist write
(POST /sync/add-to-list) needs write and a scope can never be widened without
the user authorizing again. The exchange's returned `scope` is checked for
`media:write` and the link is refused, saying why, when it is missing —
app/auth/simkl_routes.py is where that refusal lives, since only the callback
knows whether this is a first link or a routine sign-in.

REVOCATION IS DOCUMENTED NOW (POST /oauth2/revoke, api-reference/oauth2-revoke)
where V1 had none. It always answers 200 by RFC 7009 — there is no success
signal to read, so revoke_token here is fire-and-forget from its caller's point
of view: the local row is what actually stops this instance using the token.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time as _time
from urllib.parse import urlencode

import httpx

# The Simkl SOURCE's transport, not a client of this module's own. The sign-in
# exchange goes to the same host under the same published quota as every other
# Simkl call, so it belongs behind the same POST pacer and the same 412 circuit
# breaker: a token exchange fired while the instance's client id is blocked would
# otherwise be the one call that keeps hammering a service that has said stop.
# Reached through the module object rather than bound as a name at import, so
# patching app.providers.simkl.transport reaches this copy too.
from ..providers.simkl import transport
from ..providers.simkl.transport import API_BASE

# Same "app.perf" DEBUG channel every other outbound call in this app logs to.
# Never logs a body: these calls carry the client secret and a bearer token, so
# only the path and the outcome go out.
_perf = logging.getLogger("app.perf")

TOKEN_URL = f"{API_BASE}/oauth2/token"
REVOKE_URL = f"{API_BASE}/oauth2/revoke"
# The approval screen is a page a human looks at, so it lives on the website
# rather than on the API host the exchange goes to.
AUTHORIZE_URL = "https://simkl.com/oauth2/authorize"

# Both scopes from the first link. See this module's docstring for why write is
# needed even though nothing in THIS build ever writes to Simkl — the tracker's
# watchlist does, and a scope cannot be widened without asking the user again.
SCOPE = "media:read media:write"
REQUIRED_SCOPE = "media:write"

# A GET, per Simkl's API reference (measured 2026-09-27: it also answers on a
# V1 token, same {account, user} shape the historical POST returned). It
# returns the signed-in account's profile and settings, and `account.id` is the
# immutable numeric account id this app keys an identity on.
ACCOUNT_PATH = "/users/settings"
ACCOUNT_URL = f"{API_BASE}{ACCOUNT_PATH}"

# Where Simkl sends the browser back to. The operator registers exactly this
# path under their public base URL on the Simkl developer application.
CALLBACK_PATH = "/auth/simkl/callback"

# V2 access and refresh tokens are told apart from a V1 token BY SHAPE, locally
# — Simkl gives no other signal. A V1 token is 64 lowercase hex; a V2 one carries
# this prefix (measured against the plan's own research: `simkl_at_`, 43 chars).
# See simkl_routes.access_token_for_user, where a token failing this check is
# read as a legacy grant and treated as "reconnect needed" rather than sent.
V2_ACCESS_TOKEN_PREFIX = "simkl_at_"


class AccountLookupError(Exception):
    """The account endpoint could not be resolved to a usable identity."""


def redirect_uri(public_base_url: str) -> str:
    """The callback URL, built from the configured origin and nothing else."""
    return f"{(public_base_url or '').strip().rstrip('/')}{CALLBACK_PATH}"


def generate_pkce_pair() -> tuple[str, str]:
    """(verifier, challenge) for one authorization handshake.

    THE VERIFIER NEVER LEAVES THIS SERVER. It is stored on the handshake row
    (app/auth/handshakes.py's `pkce_verifier` column) and read back only when
    THIS app exchanges the code — never sent to the browser, never round-tripped
    through a cookie or the redirect URL, which is the whole point of PKCE: a
    stolen authorization code is useless without the verifier that produced its
    challenge.

    S256 ONLY — Simkl's PKCE reference documents plain as unsupported. The
    verifier is a high-entropy random string (RFC 7636 allows 43-128 chars of
    unreserved characters; token_urlsafe(32) yields 43), and the challenge is
    BASE64URL(SHA256(verifier)) with the padding stripped, exactly as the RFC
    and Simkl's own reference define it.
    """
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def authorize_url(client_id: str, public_base_url: str, state: str,
                  code_challenge: str) -> str:
    """Where to send the browser to start a redirect authorization.

    `state` is the handshake identifier. Simkl hands it back unchanged on the
    callback, which is the only thing tying the returning request to the one
    that started it. `code_challenge` is generate_pkce_pair()'s second half —
    the verifier itself never appears in this URL or anywhere client-side.
    """
    return AUTHORIZE_URL + "?" + urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri(public_base_url),
        "state": state,
        "scope": SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    })


def _oauth_headers() -> dict:
    """Headers for the OAuth calls that carry a JSON or no body (authorize-time
    account lookups). NOT for the token endpoint — see exchange_code and
    refresh_access_token, which are form-encoded and let httpx set their own
    Content-Type.

    Deliberately NOT transport.api_headers: that one attaches the INSTANCE-WIDE
    token whenever one is configured, and every call in this module is about one
    specific person — the code being exchanged, or the bearer just minted from
    it. Sending the operator's token alongside would at best be ignored and at
    worst resolve the wrong account.
    """
    return {
        "Content-Type": "application/json",
        "User-Agent": transport.USER_AGENT,
    }


def _form_headers() -> dict:
    """Headers for the form-encoded token/revoke calls. No Content-Type here —
    httpx sets application/x-www-form-urlencoded itself for `data=`, and stating
    one by hand risks it drifting from what is actually sent."""
    return {"User-Agent": transport.USER_AGENT}


async def exchange_code(
    client_id: str, client_secret: str, code: str, public_base_url: str,
    code_verifier: str,
) -> dict:
    """Trade a one-time authorization code for an access/refresh token pair.

    `redirect_uri` is sent again even though the browser has already been
    redirected: Simkl checks that the exchange comes from the same registered
    application the authorization was issued to, and rejects a mismatch.
    `code_verifier` is the plaintext PKCE secret this exchange proves possession
    of — see generate_pkce_pair.

    FORM-ENCODED, per RFC 6749's token-endpoint requirement and Simkl's own
    documented examples — measured live 2026-09-27 that the endpoint also
    accepts JSON, but there is no reason to rely on the undocumented shape.

    Routed through transport.send on the SYNC pool rather than a client of its
    own — see this module's docstring. Never transport.cached_get: that function
    is GET-only by design, and a token is the last response that should ever be
    written to a URL-keyed cache the whole instance shares.
    """
    t0 = _time.perf_counter()
    resp = await transport.send(
        transport.sync_client(), "POST", TOKEN_URL,
        pool=transport.SYNC_POOL, timeout=15, headers=_form_headers(),
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri(public_base_url),
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
        },
    )
    _perf.debug("netPOST   oauth2/token (code) -> %s  %.0fms", resp.status_code,
                (_time.perf_counter() - t0) * 1000.0)
    resp.raise_for_status()
    return resp.json()


async def refresh_access_token(client_id: str, client_secret: str,
                               refresh_token: str) -> dict:
    """Exchange a still-valid refresh_token for a new access_token + a refresh
    token — SAME ONE BACK, non-rotating (see this module's docstring), but the
    caller persists whatever this returns rather than assuming so, the same
    discipline Trakt's refresh already keeps.

    Serialization is the CALLER's job (the identity row's refresh lease in
    app/auth/simkl_routes.py.access_token_for_user) — this function only ever
    makes the one call it is asked to make.
    """
    t0 = _time.perf_counter()
    resp = await transport.send(
        transport.sync_client(), "POST", TOKEN_URL,
        pool=transport.SYNC_POOL, timeout=15, headers=_form_headers(),
        data={
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
        },
    )
    _perf.debug("netPOST   oauth2/token (refresh) -> %s  %.0fms", resp.status_code,
                (_time.perf_counter() - t0) * 1000.0)
    resp.raise_for_status()
    return resp.json()


async def revoke_token(client_id: str, client_secret: str, access_token: str) -> None:
    """Tell Simkl to invalidate an access token, so the grant disappears from
    the user's connected-apps list rather than lingering there unused.

    ALWAYS ANSWERS 200, BY RFC 7009 — "whether or not it succeeded", so there is
    no success signal in the response to check. Deleting the local row is what
    stops THIS instance using the token; this is what stops anyone using it.
    Raises on a transport failure so the caller can decide whether to tell the
    user revocation may not have reached Simkl — an unlink that could not reach
    it still succeeds locally, but only the user can finish the job on
    simkl.com, and only if they are told to.
    """
    t0 = _time.perf_counter()
    resp = await transport.send(
        transport.sync_client(), "POST", REVOKE_URL,
        pool=transport.SYNC_POOL, timeout=15, headers=_form_headers(),
        data={
            "token": access_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    _perf.debug("netPOST   oauth2/revoke -> %s  %.0fms", resp.status_code,
                (_time.perf_counter() - t0) * 1000.0)
    resp.raise_for_status()


async def fetch_account(client_id: str, access_token: str) -> dict:
    """Resolve a token to its owner: {"id": str, "name": str | None}.

    `id` is the numeric account id, and it is the ONLY acceptable key for a
    stored identity. A Simkl username is changeable by its owner and can be
    released and re-registered by somebody else, who would then inherit whatever
    account it was linked to — so a response carrying no numeric id raises rather
    than falling back to one. The name is display-only and refreshed on each
    sign-in.

    THE AVATAR URL IS ALSO READ, and this docstring used to say nothing else was
    — which stopped being true when profile pictures began being seeded from the
    provider, so it is corrected here rather than left to mislead. It is
    display-only, is never part of the identity, and is checked against a
    per-provider host allowlist before anything fetches it: see
    app/auth/provider_avatars.py, which also records why a Simkl avatar is only
    fetched when Simkl itself hosts it.

    Nothing else from this response is read or stored. It also carries the
    account's connections and plan, which have no business in this app's database
    or its logs.

    A GET, not the POST this endpoint used to be sent as. Simkl's own API
    reference now lists /users/settings as GET only, and a live GET with a V1
    token answers 200 with the identical `{account, user}` shape (measured
    2026-09-27) — so the historical POST was never required, and staying on it
    would need OAuth v2's media:write scope for a call that reads and changes
    nothing. It is under /users/, so it goes on the SYNC pool and is paced with
    every other request there.

    Raises AccountLookupError for every failure, including a network one, so the
    caller decides whether that is fatal.
    """
    # The three things Simkl asks every request to carry. Built here rather than
    # through transport.api_params because that one reads the client id off a
    # Settings object, and this flow is handed the id directly — the account being
    # looked up is the one that just signed in, not the instance's own.
    url = (f"{ACCOUNT_URL}?"
           f"{urlencode({'client_id': client_id, 'app-name': transport.APP_NAME_TRACKER, 'app-version': transport.app_version()})}")
    try:
        t0 = _time.perf_counter()
        resp = await transport.send(
            transport.sync_client(), "GET", url,
            pool=transport.SYNC_POOL, timeout=15,
            headers={**_oauth_headers(), "Authorization": f"Bearer {access_token}"},
        )
        _perf.debug("netGET    %s -> %s  %.0fms", ACCOUNT_PATH, resp.status_code,
                    (_time.perf_counter() - t0) * 1000.0)
        if resp.status_code != 200:
            raise AccountLookupError(f"Simkl {ACCOUNT_PATH} returned HTTP {resp.status_code}.")
        payload = resp.json()
    except transport.SimklError as exc:
        # A rate-limited or breaker-refused lookup is not an httpx error, so it
        # would otherwise slip past the catch below and surface raw. Fold it into
        # the same lookup failure the caller already handles — it is just another
        # reason the account could not be resolved right now.
        raise AccountLookupError(f"Simkl {ACCOUNT_PATH} could not be reached: {exc}") from exc
    except httpx.HTTPError as exc:
        raise AccountLookupError(f"Simkl {ACCOUNT_PATH} failed: {exc}") from exc
    except ValueError as exc:
        raise AccountLookupError(f"Simkl {ACCOUNT_PATH} returned a body that is not JSON.") from exc
    account = (payload or {}).get("account") or {}
    account_id = str(account.get("id") or "").strip()
    if not account_id:
        raise AccountLookupError(f"Simkl {ACCOUNT_PATH} returned no numeric account id.")
    user = (payload or {}).get("user") or {}
    return {"id": account_id, "name": user.get("name") or None,
            "avatar": user.get("avatar")}
