"""Log in with Plex — the PIN-based flow.

Plex offers no redirect/OAuth flow the way Trakt does, so the shape here is
different: request a PIN from plex.tv, send the visitor to a Plex-hosted popup
to approve it, and poll plex.tv until the PIN carries an auth token. No
admin-side app registration and no redirect URI are involved. The only thing
this app supplies is an X-Plex-Client-Identifier — a UUID naming this
INSTALLATION, not any particular user, generated once and persisted in
app_meta — and a product-name header.

  1. POST /api/v2/pins -> {id, code}
  2. Send the browser to app.plex.tv/auth with that code, in a popup.
  3. Poll GET /api/v2/pins/<id> until it carries a non-null authToken.
  4. GET /api/v2/user with that token -> the account's immutable numeric id.

A completed Plex login proves only that the visitor controls SOME plex.tv
account — it is not a membership check against any particular server, and the
intended users of this app are not expected to be on this instance's Plex
server. Registration is gated by the invite system the same way Trakt's is.
"""
from __future__ import annotations

import uuid
from urllib.parse import urlencode

import httpx

from . import db

BASE_URL = "https://plex.tv"
PINS_URL = f"{BASE_URL}/api/v2/pins"
ACCOUNT_URL = f"{BASE_URL}/api/v2/user"
# The human-facing approval screen lives on the app site, not the API host.
AUTH_APP_URL = "https://app.plex.tv/auth"

# Shown to Plex as the name of the thing asking for access. Cosmetic only — it
# appears on the approval screen and in the user's plex.tv device list.
PRODUCT = "Trakt New Shows"

CLIENT_IDENTIFIER_META_KEY = "plex_client_identifier"


class PinError(Exception):
    """A PIN could not be requested from, or resolved against, plex.tv."""


class AccountLookupError(Exception):
    """The Plex account behind a linked PIN could not be resolved."""


async def ensure_client_identifier() -> str:
    """This installation's X-Plex-Client-Identifier, generating and persisting
    one the first time anything asks.

    Idempotent under a race: the insert is conditional, so two callers that
    both find nothing at once still converge on whichever value the database
    kept, rather than each instance quietly using its own.
    """
    existing = await db.get_meta(CLIENT_IDENTIFIER_META_KEY)
    if existing:
        return existing
    candidate = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO app_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO NOTHING",
        (CLIENT_IDENTIFIER_META_KEY, candidate),
    )
    return await db.get_meta(CLIENT_IDENTIFIER_META_KEY, candidate)


def _headers(client_id: str) -> dict:
    return {
        "Accept": "application/json",
        "X-Plex-Client-Identifier": client_id,
        "X-Plex-Product": PRODUCT,
    }


async def request_pin(client_id: str) -> dict:
    """Ask plex.tv for a new PIN. Returns {"id": int, "code": str}.

    `strong=true` asks for a PIN that also yields a full auth token once
    linked, rather than the weaker four-digit code meant for TV-style manual
    entry — this flow reads the token straight off the poll response instead.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(PINS_URL, headers=_headers(client_id), data={"strong": "true"})
    if resp.status_code not in (200, 201):
        raise PinError(f"plex.tv PIN request returned HTTP {resp.status_code}.")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise PinError("plex.tv PIN request returned a body that is not JSON.") from exc
    pin_id, code = payload.get("id"), payload.get("code")
    if not (isinstance(pin_id, int) and code):
        raise PinError("plex.tv PIN request returned no id/code.")
    return {"id": pin_id, "code": code}


def popup_url(client_id: str, code: str) -> str:
    """Where to send the popup window for the visitor to approve the PIN."""
    query = urlencode({
        "clientID": client_id,
        "code": code,
        "context[device][product]": PRODUCT,
    })
    return f"{AUTH_APP_URL}#?{query}"


async def poll_pin(pin_id: int, client_id: str) -> str | None:
    """Check a PIN's status. Returns the auth token once approved, or None
    while still waiting.

    Raises PinError once the PIN is unknown or expired at plex.tv — its own
    lifetime there is independent of this app's handshake row, and either can
    lapse first.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{PINS_URL}/{pin_id}", headers=_headers(client_id))
    if resp.status_code == 404:
        raise PinError("This sign-in code is no longer valid.")
    if resp.status_code != 200:
        raise PinError(f"plex.tv PIN status returned HTTP {resp.status_code}.")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise PinError("plex.tv PIN status returned a body that is not JSON.") from exc
    return payload.get("authToken") or None


async def fetch_account(auth_token: str, client_id: str) -> dict:
    """Resolve an auth token to its owner: {"id": int, "name": str | None}.

    `id` is Plex's immutable numeric account id and is the only acceptable key
    for a stored identity — a username or email can be changed by its owner
    and later reused by somebody else, who would then inherit whatever this
    app had linked to it. Raises AccountLookupError for every failure,
    including a response carrying no numeric id, exactly as the Trakt
    equivalent does.
    """
    headers = _headers(client_id)
    headers["X-Plex-Token"] = auth_token
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(ACCOUNT_URL, headers=headers)
        if resp.status_code != 200:
            raise AccountLookupError(f"plex.tv /api/v2/user returned HTTP {resp.status_code}.")
        payload = resp.json()
    except httpx.HTTPError as exc:
        raise AccountLookupError(f"plex.tv /api/v2/user failed: {exc}") from exc
    except ValueError as exc:
        raise AccountLookupError("plex.tv /api/v2/user returned a body that is not JSON.") from exc
    account_id = payload.get("id")
    if not isinstance(account_id, int):
        raise AccountLookupError("plex.tv /api/v2/user returned no numeric account id.")
    name = payload.get("username") or payload.get("title") or payload.get("email")
    return {"id": account_id, "name": name}
