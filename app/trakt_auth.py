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

from urllib.parse import urlencode

import httpx

from .trakt import API_BASE

DEVICE_CODE_URL = f"{API_BASE}/oauth/device/code"
DEVICE_TOKEN_URL = f"{API_BASE}/oauth/device/token"
TOKEN_URL = f"{API_BASE}/oauth/token"
# The authorization screen is a page a human looks at, so it lives on the site
# rather than on the API host every other call here uses.
AUTHORIZE_URL = "https://trakt.tv/oauth/authorize"
ACCOUNT_URL = f"{API_BASE}/users/me"

# Where Trakt sends the browser back to. The operator registers exactly this
# path under their public base URL on the Trakt API application.
CALLBACK_PATH = "/auth/trakt/callback"


class AccountLookupError(Exception):
    """`/users/me` could not be resolved to a usable identity."""


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
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(TOKEN_URL, json={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri(public_base_url),
            "grant_type": "authorization_code",
        })
    resp.raise_for_status()
    return resp.json()


async def fetch_account(client_id: str, access_token: str) -> dict:
    """Resolve a token to its owner: {"id": int, "name": str | None}.

    `id` is Trakt's immutable numeric account id, and it is the ONLY acceptable
    key for a stored identity. A username or slug is changeable by its owner and
    can be released and re-registered by somebody else, who would then inherit
    whatever account it was linked to — so a response that carries no numeric id
    raises rather than falling back to one. The name is for display only and is
    refreshed on each sign-in.

    Raises AccountLookupError for every failure, including a network one, so the
    caller decides whether that is fatal.
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                ACCOUNT_URL,
                params={"extended": "full"},
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "trakt-api-version": "2",
                    "trakt-api-key": client_id,
                },
            )
        if resp.status_code != 200:
            raise AccountLookupError(f"Trakt /users/me returned HTTP {resp.status_code}.")
        payload = resp.json()
    except httpx.HTTPError as exc:
        raise AccountLookupError(f"Trakt /users/me failed: {exc}") from exc
    except ValueError as exc:
        raise AccountLookupError("Trakt /users/me returned a body that is not JSON.") from exc
    trakt_id = ((payload or {}).get("ids") or {}).get("trakt")
    if not isinstance(trakt_id, int):
        raise AccountLookupError("Trakt /users/me returned no numeric account id.")
    return {"id": trakt_id, "name": payload.get("name") or payload.get("username")}


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
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(DEVICE_CODE_URL, json={"client_id": client_id})
    resp.raise_for_status()
    return resp.json()


async def poll_device_token(client_id: str, client_secret: str, device_code: str) -> dict:
    """Check whether the user has approved `device_code` yet.

    Returns the token payload on success; raises one of the Device* exceptions
    for every other documented status (400/404/409/410/418/429) so the caller
    can distinguish "still waiting" from "give up and restart".
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(DEVICE_TOKEN_URL, json={
            "code": device_code,
            "client_id": client_id,
            "client_secret": client_secret,
        })
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
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(TOKEN_URL, json={
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
        })
    resp.raise_for_status()
    return resp.json()
