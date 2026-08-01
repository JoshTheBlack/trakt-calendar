"""Trakt OAuth — the two authorization flows, refresh-token renewal, and the
account lookup that turns a token into an identity.

REDIRECT (authorization_code) is what an ordinary user meets. Their browser is
sent to trakt.tv/oauth/authorize, they approve, and Trakt redirects back to this
app's callback with a one-time `code` which is exchanged for a token pair. It is
one click, and it is what "Log in with Trakt" is built on.

  1. Redirect the browser to authorize_url(...).
  2. Trakt redirects back to `redirect_uri` with ?code=&state=.
  3. POST /oauth/token {code, client_id, client_secret, redirect_uri,
     grant_type: "authorization_code"} -> the same token payload the device flow
     returns.

`redirect_uri` must match the value registered on the Trakt API application
EXACTLY, and Trakt compares it again during the exchange — which is why it is
built from the configured public base URL and never from a request header.

Trakt does not support PKCE: `/oauth/authorize` documents only response_type,
client_id, redirect_uri, and state, and the token exchange documents no
code_verifier. A code is therefore protected by the client secret plus the
server-side binding on `state`, and nothing here sends a challenge Trakt would
silently ignore.

DEVICE CODE is kept as the administrator's break-glass path for the app-wide
connection. It needs no redirect URI at all, so it still works when the
registered one is wrong or the public base URL is unset — which is exactly the
situation that would otherwise lock an operator out of re-authorizing.

  1. POST /oauth/device/code {client_id} -> {device_code, user_code,
     verification_url, expires_in, interval}
  2. User opens verification_url in a browser and enters user_code.
  3. Poll POST /oauth/device/token {code, client_id, client_secret} every
     `interval` seconds until the user approves (200), or the code expires/is
     denied. Success returns {access_token, refresh_token, expires_in,
     created_at, token_type, scope}.

Later, POST /oauth/token {refresh_token, client_id, client_secret,
grant_type: "refresh_token"} exchanges a still-valid refresh_token for a new
access_token + refresh_token pair (Trakt issues a new refresh_token on every
refresh — the caller MUST persist the new one, the old one stops working).
"""
from __future__ import annotations

import logging
import time as _time
from urllib.parse import urlencode

import httpx

# The OAuth endpoints are NOT the Trakt data API: they carry a client_id/secret
# in the body and must NOT carry the API's Authorization/trakt-api-key headers,
# so what they want is the bare sender — one request plus the 429 retry/backoff
# budget, and no interpretation — rather than transport.cached_get. Reached
# through the module object rather than bound as a name at import, so that
# patching app.providers.trakt.transport.send reaches this copy too.
# The Trakt SOURCE's transport, not this package's: the login flow exchanges its
# codes on the same pooled client and against the same API base the source uses,
# so the two cannot drift apart.
from ..providers.trakt import transport
from ..providers.trakt.transport import API_BASE, TraktRateLimitError
from .. import http_pool

# OAUTH GETS ITS OWN POOL, SEPARATE FROM THE TRAKT DATA API'S, and that is the
# one deliberate exception to "one pool per service" in this app. These calls go
# to the same host as every other Trakt request, but they must not queue behind
# them: the token refresh on the heartbeat is what keeps the app's Trakt access
# alive, and letting it wait on a distrakt fan-out that has taken every
# connection is how an instance loses its authorization while "just" being busy.
# A sign-in is the same story from the user's side.
#
# NO CONCURRENCY GATE, unlike the data pool: there are never more than a handful
# of these in flight, and pacing the call that renews the credentials would be
# protecting Trakt's quota at the expense of the thing that must not fail. The
# 429 retry in transport.send still applies — these calls route through it.
POOL = http_pool.Pool("trakt_auth", max_connections=4, timeout=15)

# Same "app.perf" DEBUG channel the Trakt transport's cached_get and
# app/calendar/cache.py's fetch_window_raw log their own outbound calls to —
# one place to watch every Trakt request, OAuth included. Never logs a body:
# these calls carry client_secret/tokens, so only the path and outcome go out.
_perf = logging.getLogger("app.perf")

DEVICE_CODE_URL = f"{API_BASE}/oauth/device/code"
DEVICE_TOKEN_URL = f"{API_BASE}/oauth/device/token"
TOKEN_URL = f"{API_BASE}/oauth/token"
REVOKE_URL = f"{API_BASE}/oauth/revoke"
# The authorization screen is a page a human looks at, so it lives on the site
# rather than on the API host every other call here uses.
AUTHORIZE_URL = "https://trakt.tv/oauth/authorize"
# NOT /users/me. A Trakt user's `ids` object is `{"slug": ...}` — there is no
# numeric user id anywhere in the API, and /users/me does not expose the UUID
# either. /users/settings returns the same user object WITH `ids.uuid`, which is
# the only stable, non-reassignable handle Trakt gives out for an account.
ACCOUNT_PATH = "/users/settings"
ACCOUNT_URL = f"{API_BASE}{ACCOUNT_PATH}"

# Where Trakt sends the browser back to. The operator registers exactly this
# path under their public base URL on the Trakt API application.
CALLBACK_PATH = "/auth/trakt/callback"


class AccountLookupError(Exception):
    """The account endpoint could not be resolved to a usable identity."""


def redirect_uri(public_base_url: str) -> str:
    """The callback URL, built from the configured origin and nothing else."""
    return f"{(public_base_url or '').strip().rstrip('/')}{CALLBACK_PATH}"


def authorize_url(client_id: str, public_base_url: str, state: str) -> str:
    """Where to send the browser to start a redirect authorization.

    `state` is the handshake identifier. Trakt hands it back unchanged on the
    callback, which is the only thing tying the returning request to the one
    that started it.
    """
    return AUTHORIZE_URL + "?" + urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri(public_base_url),
        "state": state,
    })


async def exchange_code(
    client_id: str, client_secret: str, code: str, public_base_url: str,
) -> dict:
    """Trade a one-time authorization code for an access/refresh token pair.

    `redirect_uri` is sent again even though the browser has already been
    redirected: Trakt checks that the exchange comes from the same registered
    application the authorization was issued to, and rejects a mismatch.
    """
    t0 = _time.perf_counter()
    client = POOL.client()
    resp = await transport.send(client, "POST", TOKEN_URL, timeout=15, json={
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri(public_base_url),
        "grant_type": "authorization_code",
    })
    _perf.debug("netPOST   oauth/token (code) -> %s  %.0fms", resp.status_code,
                (_time.perf_counter() - t0) * 1000.0)
    resp.raise_for_status()
    return resp.json()


async def fetch_account(client_id: str, access_token: str) -> dict:
    """Resolve a token to its owner: {"id": str, "name": str | None}.

    `id` is the account's UUID, and it is the ONLY acceptable key for a stored
    identity. A username or slug is changeable by its owner and can be released
    and re-registered by somebody else, who would then inherit whatever account
    it was linked to — so a response carrying no UUID raises rather than falling
    back to one. The name is display-only and refreshed on each sign-in.

    WHY /users/settings AND NOT /users/me: Trakt users have no numeric id. The
    `ids` object on a user is `{"slug": ...}` — and `/users/me?extended=full`
    returns exactly that and nothing more, on every account, verified live. The
    UUID is only exposed by `/users/settings`, which returns the same user object
    with `ids: {slug, uuid}`. Keying on a numeric id here was never merely
    unavailable, it was impossible, and it made every Trakt sign-in fail with an
    account-lookup error.

    Nothing else from this response is read or stored. It also carries
    `account.token`, an unrelated Trakt-internal value, which has no business in
    this app's database or its logs.

    Raises AccountLookupError for every failure, including a network one, so the
    caller decides whether that is fatal.
    """
    try:
        t0 = _time.perf_counter()
        client = POOL.client()
        resp = await transport.send(
            client, "GET", ACCOUNT_URL, timeout=15,
            headers={
                "Authorization": f"Bearer {access_token}",
                "trakt-api-version": "2",
                "trakt-api-key": client_id,
            },
        )
        _perf.debug("netGET    %s -> %s  %.0fms", ACCOUNT_PATH, resp.status_code,
                    (_time.perf_counter() - t0) * 1000.0)
        if resp.status_code != 200:
            raise AccountLookupError(f"Trakt {ACCOUNT_PATH} returned HTTP {resp.status_code}.")
        payload = resp.json()
    except TraktRateLimitError as exc:
        # An exhausted-retry 429 is not an httpx error, so it would otherwise slip
        # past the httpx.HTTPError catch below and surface raw. Fold it into the
        # same lookup failure the caller already handles — a rate-limited identity
        # lookup is just another reason the account couldn't be resolved right now.
        raise AccountLookupError(f"Trakt {ACCOUNT_PATH} was rate-limited: {exc}") from exc
    except httpx.HTTPError as exc:
        raise AccountLookupError(f"Trakt {ACCOUNT_PATH} failed: {exc}") from exc
    except ValueError as exc:
        raise AccountLookupError(f"Trakt {ACCOUNT_PATH} returned a body that is not JSON.") from exc
    user = (payload or {}).get("user") or {}
    uuid = ((user.get("ids") or {}).get("uuid") or "").strip()
    if not uuid:
        raise AccountLookupError(f"Trakt {ACCOUNT_PATH} returned no account UUID.")
    return {"id": uuid, "name": user.get("name") or user.get("username")}


class DevicePending(Exception):
    """The user hasn't approved (or denied) the code yet — keep polling."""


class DeviceSlowDown(Exception):
    """Polling too fast — back off (Trakt asked for a slower interval)."""


class DeviceExpired(Exception):
    """The device code is invalid or expired — the user must restart the flow."""


class DeviceDenied(Exception):
    """The user denied the authorization request, or the code was already used."""


async def request_device_code(client_id: str) -> dict:
    """Start a device-auth session. Returns the raw Trakt payload (device_code,
    user_code, verification_url, expires_in, interval)."""
    t0 = _time.perf_counter()
    client = POOL.client()
    resp = await transport.send(client, "POST", DEVICE_CODE_URL, timeout=15, json={"client_id": client_id})
    _perf.debug("netPOST   oauth/device/code -> %s  %.0fms", resp.status_code,
                (_time.perf_counter() - t0) * 1000.0)
    resp.raise_for_status()
    return resp.json()


async def poll_device_token(client_id: str, client_secret: str, device_code: str) -> dict:
    """Check whether the user has approved `device_code` yet.

    Returns the token payload on success; raises one of the Device* exceptions
    for every other documented status (400/404/409/410/418/429) so the caller
    can distinguish "still waiting" from "give up and restart".
    """
    t0 = _time.perf_counter()
    client = POOL.client()
    resp = await client.post(DEVICE_TOKEN_URL, json={
        "code": device_code,
        "client_id": client_id,
        "client_secret": client_secret,
    })
    _perf.debug("netPOST   oauth/device/token -> %s  %.0fms", resp.status_code,
                (_time.perf_counter() - t0) * 1000.0)
    if resp.status_code == 200:
        return resp.json()
    if resp.status_code == 400:
        raise DevicePending("Waiting for the user to authorize the code.")
    if resp.status_code == 404:
        raise DeviceExpired("Invalid device code.")
    if resp.status_code == 409:
        raise DeviceDenied("This code has already been used.")
    if resp.status_code == 410:
        raise DeviceExpired("The device code expired — start over.")
    if resp.status_code == 418:
        raise DeviceDenied("Authorization was denied.")
    if resp.status_code == 429:
        raise DeviceSlowDown("Polling too fast — slow down.")
    resp.raise_for_status()
    return resp.json()  # pragma: no cover — unreachable once raise_for_status raises


async def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> dict:
    """Exchange a refresh_token for a new access_token + refresh_token pair."""
    t0 = _time.perf_counter()
    client = POOL.client()
    resp = await transport.send(client, "POST", TOKEN_URL, timeout=15, json={
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
    })
    _perf.debug("netPOST   oauth/token (refresh) -> %s  %.0fms", resp.status_code,
                (_time.perf_counter() - t0) * 1000.0)
    resp.raise_for_status()
    return resp.json()


async def revoke_token(client_id: str, client_secret: str, access_token: str) -> None:
    """Tell Trakt to invalidate an access token, so the grant disappears from the
    user's "connected apps" list rather than lingering there unused.

    Deleting the local row is what stops this instance using a token; this is
    what stops anyone using it. Raises on any failure so the caller can say so —
    an unlink that could not reach Trakt still succeeded locally, but the user is
    the only one who can finish the job on trakt.tv, and they can only do that if
    they are told.
    """
    t0 = _time.perf_counter()
    client = POOL.client()
    resp = await transport.send(client, "POST", REVOKE_URL, timeout=15, json={
        "token": access_token,
        "client_id": client_id,
        "client_secret": client_secret,
    })
    _perf.debug("netPOST   oauth/revoke -> %s  %.0fms", resp.status_code,
                (_time.perf_counter() - t0) * 1000.0)
    resp.raise_for_status()
