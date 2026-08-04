"""Simkl OAuth — the redirect authorization flow and the account lookup that
turns a token into an identity.

One flow, not two. Simkl documents an authorization-code redirect and nothing
else: there is no device-code path here the way there is for Trakt, so there is
no break-glass route for an operator whose registered redirect URI is wrong. The
only fix for that is to correct it on simkl.com, which is why the Settings screen
shows the exact URI to register rather than leaving the operator to build it.

  1. Redirect the browser to authorize_url(...).
  2. Simkl redirects back to `redirect_uri` with ?code=&state=.
  3. POST /oauth/token {client_id, client_secret, code, redirect_uri,
     grant_type: "authorization_code"} -> {access_token, token_type, scope,
     expires_in}.

`redirect_uri` must match the value registered on the Simkl developer
application EXACTLY — Simkl compares it byte for byte, and again during the
exchange — which is why it is built from the configured public base URL and
never from a request header.

THERE IS NO REFRESH TOKEN, AND THAT IS A FACT ABOUT SIMKL RATHER THAN AN
OMISSION HERE. The exchange returns an access token with a nominal lifetime of
about five years and no refresh token at all; the grant stands until the user
revokes the application on Simkl's side. So this module has no analogue of
Trakt's refresh_access_token, an identity minted from it stores NULL for both
`refresh_token` and `token_expires_at`, and the answer to a 401 is to link
again — there is nothing to renew.

Simkl also documents no token revocation endpoint, so unlinking here removes the
local row and nothing more. A user who wants the grant itself gone removes it
under their Simkl account settings.
"""
from __future__ import annotations

import logging
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

TOKEN_URL = f"{API_BASE}/oauth/token"
# The approval screen is a page a human looks at, so it lives on the website
# rather than on the API host the exchange goes to.
AUTHORIZE_URL = "https://simkl.com/oauth/authorize"

# A POST, with no body, for historical reasons — Simkl's own docs say so. It
# returns the signed-in account's profile and settings, and `account.id` is the
# immutable numeric account id this app keys an identity on.
ACCOUNT_PATH = "/users/settings"
ACCOUNT_URL = f"{API_BASE}{ACCOUNT_PATH}"

# Where Simkl sends the browser back to. The operator registers exactly this
# path under their public base URL on the Simkl developer application.
CALLBACK_PATH = "/auth/simkl/callback"


class AccountLookupError(Exception):
    """The account endpoint could not be resolved to a usable identity."""


def redirect_uri(public_base_url: str) -> str:
    """The callback URL, built from the configured origin and nothing else."""
    return f"{(public_base_url or '').strip().rstrip('/')}{CALLBACK_PATH}"


def authorize_url(client_id: str, public_base_url: str, state: str) -> str:
    """Where to send the browser to start a redirect authorization.

    `state` is the handshake identifier. Simkl hands it back unchanged on the
    callback, which is the only thing tying the returning request to the one
    that started it.
    """
    return AUTHORIZE_URL + "?" + urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri(public_base_url),
        "state": state,
    })


def _oauth_headers() -> dict:
    """Headers for the OAuth calls.

    Deliberately NOT transport.api_headers: that one attaches the INSTANCE-WIDE
    token whenever one is configured, and every call in this module is about one
    specific person — the code being exchanged, or the bearer just minted from
    it. Sending the operator's token alongside would at best be ignored and at
    worst resolve the wrong account.
    """
    return {
        "Content-Type": "application/json",
        "User-Agent": transport.USER_AGENT,
        "app-name": transport.APP_NAME,
        "app-version": transport.APP_VERSION,
    }


async def exchange_code(
    client_id: str, client_secret: str, code: str, public_base_url: str,
) -> dict:
    """Trade a one-time authorization code for an access token.

    `redirect_uri` is sent again even though the browser has already been
    redirected: Simkl checks that the exchange comes from the same registered
    application the authorization was issued to, and rejects a mismatch.

    Routed through transport.send on the SYNC pool rather than a client of its
    own — see this module's docstring. Never transport.cached_get: that function
    is GET-only by design, and a token is the last response that should ever be
    written to a URL-keyed cache the whole instance shares.
    """
    t0 = _time.perf_counter()
    resp = await transport.send(
        transport.sync_client(), "POST", TOKEN_URL,
        pool=transport.SYNC_POOL, timeout=15, headers=_oauth_headers(),
        json={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri(public_base_url),
            "grant_type": "authorization_code",
        },
    )
    _perf.debug("netPOST   oauth/token (code) -> %s  %.0fms", resp.status_code,
                (_time.perf_counter() - t0) * 1000.0)
    resp.raise_for_status()
    return resp.json()


async def fetch_account(client_id: str, access_token: str) -> dict:
    """Resolve a token to its owner: {"id": str, "name": str | None}.

    `id` is the numeric account id, and it is the ONLY acceptable key for a
    stored identity. A Simkl username is changeable by its owner and can be
    released and re-registered by somebody else, who would then inherit whatever
    account it was linked to — so a response carrying no numeric id raises rather
    than falling back to one. The name is display-only and refreshed on each
    sign-in.

    Nothing else from this response is read or stored. It also carries the
    account's connections and plan, which have no business in this app's database
    or its logs.

    A POST with no body, because that is what Simkl documents for this endpoint.
    It is under /users/, so it goes on the SYNC pool and is paced with every
    other POST.

    Raises AccountLookupError for every failure, including a network one, so the
    caller decides whether that is fatal.
    """
    url = f"{ACCOUNT_URL}?{urlencode({'client_id': client_id})}"
    try:
        t0 = _time.perf_counter()
        resp = await transport.send(
            transport.sync_client(), "POST", url,
            pool=transport.SYNC_POOL, timeout=15,
            headers={**_oauth_headers(), "Authorization": f"Bearer {access_token}"},
        )
        _perf.debug("netPOST   %s -> %s  %.0fms", ACCOUNT_PATH, resp.status_code,
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
    return {"id": account_id, "name": user.get("name") or None}
