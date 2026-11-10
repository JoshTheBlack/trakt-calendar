"""TMDB's v4 user authorization — the calls behind "Connect TMDB": asking for a
request token, trading an approved one for the person's own access token,
reading which account it names, and revoking it.

A LINK, NEVER A SIGN-IN. TMDB is a catalogue this app reads with the instance's
own token; linking a person's TMDB account lets the tracker write their TMDB
watchlist and a rankings board seed from their TMDB ratings, and nothing else.
No route here or in tmdb_routes.py signs anybody in, and
app/auth/revocation.py's table says TMDB is not a way in.

THE FLOW, MEASURED against the live service on 2026-11-08:
  1. POST /4/auth/request_token {redirect_to}, authenticated with the INSTANCE's
     read token -> {request_token}: a JWT that expires 15 minutes after it was
     made and carries `redirect_to` inside it.
  2. The browser goes to https://www.themoviedb.org/auth/access?request_token=...
     and the person approves there.
  3. TMDB sends the browser to `redirect_to` EXACTLY AS GIVEN — its query string
     kept, NOTHING appended. So this app's handshake state rides inside
     redirect_to, and the request token never comes back from the browser at all:
     it stays on the handshake row, server-side.
  4. POST /4/auth/access_token {request_token} (instance token) -> {access_token,
     account_id}: the person's user token (a 304-character JWT) and their
     ACCOUNT OBJECT ID — 24 hex characters, permanent, and what TMDB's account
     reads are addressed by, so it is the identity's provider_user_id. An
     unapproved request token is refused here. Exchanging the same approved
     request token again answers the SAME access token, which is why the handshake
     row, not TMDB, is what makes a callback single-use.
  5. GET /3/account with the user token -> the account's `username` (its pages'
     address on themoviedb.org) and numeric id. A display name only.
  6. Revoke: DELETE /4/auth/access_token {access_token} (instance token) -> 200,
     status_code 13; a token already revoked answers 404, status_code 34 —
     nothing left to revoke, which is success to an unlink. A revoked token then
     answers 401 to every call.
  The token does not expire on a clock TMDB publishes, and there is no refresh:
  a grant lasts until it is revoked.

EVERY CALL GOES THROUGH THE TMDB TRANSPORT's private sender — the same gate and
429 circuit as every catalogue read, and never the shared response cache — and
no token, request token or account id is ever logged: only the step and its
status.
"""
from __future__ import annotations

import re
from typing import NamedTuple
from urllib.parse import urlencode

from ..config import Settings
from ..providers.tmdb import transport

# Where TMDB sends the browser back to, under this instance's public base URL.
CALLBACK_PATH = "/auth/tmdb/callback"
APPROVE_URL = "https://www.themoviedb.org/auth/access"
# A v4 account id: an object id, 24 hex characters (step 4 below).
ACCOUNT_ID = re.compile(r"[0-9a-f]{24}")


class TmdbAuthError(Exception):
    """A step of the authorization could not be completed. Its text names the
    step and the status, never a value that was sent."""


class Grant(NamedTuple):
    """What an approved authorization yields."""
    access_token: str
    account_id: str      # TMDB's account object id: what account reads address


class Account(NamedTuple):
    """Who a grant belongs to, as TMDB's account answer says."""
    username: str


def redirect_to(public_base_url: str, state: str) -> str:
    """The callback address, built from the configured public origin and the
    handshake's state, and from NOTHING in the request — a forwarded Host header
    could otherwise send a person's approval to somebody else's address."""
    base = (public_base_url or "").strip().rstrip("/")
    return f"{base}{CALLBACK_PATH}?{urlencode({'state': state})}"


def approve_url(request_token: str) -> str:
    """Where the browser approves the request on themoviedb.org."""
    return f"{APPROVE_URL}?{urlencode({'request_token': request_token})}"


async def _json(method: str, url: str, path: str, *, token: str, body: dict | None = None):
    """One step's answer body, or TmdbAuthError — for TMDB not answering as well
    as for an answer that is not a document."""
    try:
        resp = await transport.send_private(method, url, path, token=token, body=body)
    except transport.TmdbError as exc:
        raise TmdbAuthError(f"TMDB could not be asked ({path}).") from exc
    try:
        answer = resp.json()
    except ValueError:
        answer = None
    return resp.status_code, answer if isinstance(answer, dict) else {}


async def request_token(settings: Settings, callback: str) -> str:
    """Step 1: a request token whose approval sends the browser to `callback`."""
    status, answer = await _json(
        "POST", f"{transport.API_V4}/auth/request_token", "auth/request_token",
        token=transport.instance_token(settings), body={"redirect_to": callback})
    token = str(answer.get("request_token") or "")
    if status != 200 or answer.get("success") is not True or not token:
        raise TmdbAuthError(f"TMDB refused a request token (HTTP {status}).")
    return token


async def exchange(settings: Settings, request_token_value: str) -> Grant:
    """Step 4: the person's own access token and account for an APPROVED request
    token. Refused (TmdbAuthError) for one the person declined or never saw."""
    status, answer = await _json(
        "POST", f"{transport.API_V4}/auth/access_token", "auth/access_token",
        token=transport.instance_token(settings), body={"request_token": request_token_value})
    token = str(answer.get("access_token") or "")
    account_id = str(answer.get("account_id") or "").strip()
    if status != 200 or answer.get("success") is not True or not token or not account_id:
        raise TmdbAuthError(f"TMDB did not grant an access token (HTTP {status}).")
    # The id becomes the identity's key and a segment of every account address, so
    # it is held to the shape TMDB was measured giving rather than trusted whole.
    if not ACCOUNT_ID.fullmatch(account_id):
        raise TmdbAuthError("TMDB granted a token for an account id of an unknown shape.")
    return Grant(token, account_id)


async def fetch_account(access_token: str) -> Account:
    """Step 5: the account's username, for the account page and the watchlist's
    address. Nothing else from the answer is kept — it also carries the
    account's regional and content settings, which are no business of this
    app's."""
    status, answer = await _json("GET", f"{transport.API_BASE}/account", "account",
                                 token=access_token)
    if status != 200:
        raise TmdbAuthError(f"TMDB's account read failed (HTTP {status}).")
    return Account(str(answer.get("username") or "").strip())


async def revoke(settings: Settings, access_token: str) -> None:
    """Step 6: end the grant at TMDB. A token TMDB no longer holds (404, status
    code 34) is already ended, which is what an unlink wants; anything else that
    is not 200 raises, so the caller can tell the person to finish the job on
    themoviedb.org."""
    status, answer = await _json(
        "DELETE", f"{transport.API_V4}/auth/access_token", "auth/access_token",
        token=transport.instance_token(settings), body={"access_token": access_token})
    if status == 200 or (status == 404 and answer.get("status_code") == 34):
        return
    raise TmdbAuthError(f"TMDB did not revoke the grant (HTTP {status}).")
