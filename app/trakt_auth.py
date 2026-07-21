"""Trakt OAuth — device-code authorization + refresh-token renewal.

Trakt's device-code flow (https://trakt.docs.apiary.io -> Authentication -> Device)
is the right fit here: there's no public redirect URI for this app to register, so
the standard authorization-code flow doesn't apply. The flow:

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

import httpx

from .trakt import API_BASE

DEVICE_CODE_URL = f"{API_BASE}/oauth/device/code"
DEVICE_TOKEN_URL = f"{API_BASE}/oauth/device/token"
TOKEN_URL = f"{API_BASE}/oauth/token"


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
