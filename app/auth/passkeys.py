"""Passkeys: one more way into an account that already exists.

A passkey is a key pair an authenticator (a phone, a laptop's Windows Hello, a
password manager) makes for this instance: the private half never leaves the
device, the public half is stored here (`passkeys`, migration 50), and signing
in is the device signing a challenge this app chose. It is BESIDE an account's
password and its Trakt, Simkl and Plex sign-ins, never a second factor and never
a way to make an account: one is added from the Account page by somebody signed
in who proves the account's password first (routes.py's `begin_adding_passkey`
says why), and removed there or by an administrator. Which ways in are left
without one is identities.ways_in's answer, as for every other way in.

THE RELYING PARTY IS THE PUBLIC BASE URL, never the Host header: the passkey is
bound to that origin's host, and every response is verified against that origin
exactly (`relying_party`). A browser offers passkeys only in a secure context on
a domain name, so an instance whose base URL is not https — or http on
localhost — offers none at all.

THE CHALLENGE IS A HANDSHAKE (app/auth/handshakes.py), created before the press
and consumed once at the finish, and it is DERIVED FROM THE HANDSHAKE'S STATE
(`challenge_of`) rather than stored. A challenge is not a secret — the page
carries it — and storing nothing means nothing is sealed, so signing in works
while the at-rest key is wrong, exactly as a password sign-in does: /login is
how an administrator reaches the recovery screen. The state rides in a cookie of
its own (`ATTEMPT_COOKIE`), never in the body, so a finish names the challenge it
answers only through the browser that asked for it — and a passkey attempt never
overwrites a Trakt, Simkl or Plex sign-in in progress in another tab.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import re
import secrets
from typing import Any, NamedTuple
from urllib.parse import urlsplit

from fastapi import Request, Response
from webauthn import (generate_authentication_options, generate_registration_options,
                      options_to_json, verify_authentication_response,
                      verify_registration_response)
from webauthn.helpers import (base64url_to_bytes, bytes_to_base64url,
                              parse_authentication_credential_json,
                              parse_registration_credential_json)
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (AttestationConveyancePreference,
                                      AuthenticatorSelectionCriteria,
                                      AuthenticatorTransport, PublicKeyCredentialDescriptor,
                                      ResidentKeyRequirement, UserVerificationRequirement)

from .. import chrome, db
from ..config import Settings
from . import cookies, handshakes, identities

logger = logging.getLogger(__name__)

# What a handshake row for a passkey says it is for (auth_handshakes.provider).
PROVIDER = "passkey"

# At most this many passkeys an account: enough for every device somebody owns
# and a spare, few enough that a stolen session cannot fill the table.
MAX_PER_ACCOUNT = 20
NAME_MAX = 60

# How long the browser's prompt may stay open. The handshake behind it lives
# HANDSHAKE_TTL_SECONDS; this is shorter so a prompt never outlives its row.
PROMPT_MS = 5 * 60 * 1000

# ONE SENTENCE FOR EVERY REFUSAL AT A SIGN-IN: an unknown passkey, a removed one,
# one of another account, a bad signature, a disabled account and a locked-out
# address alike — so a refusal never says whether a passkey is known here.
REFUSED = "That passkey didn't sign you in."


class Refused(Exception):
    """A passkey ceremony that did not verify. Its message is the sentence to
    say: `REFUSED` at a sign-in, the reason at an add."""


# ---------------------------------------------------------------------------
# the relying party
# ---------------------------------------------------------------------------

class RelyingParty(NamedTuple):
    rp_id: str
    origin: str


def relying_party(settings: Settings) -> RelyingParty | None:
    """Who passkeys are made for on this instance — its public base URL's host
    and that URL exactly as an origin — or None where a browser would refuse to
    make one: no base URL, an address that is not a domain name, or plain http
    anywhere but localhost (a secure context, as browsers count one)."""
    base = (settings.public_base_url or "").strip().rstrip("/")
    if not base:
        return None
    parts = urlsplit(base)
    host = (parts.hostname or "").lower()
    if not host:
        return None
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    local = host == "localhost" or host.endswith(".localhost")
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        return None
    return RelyingParty(rp_id=host, origin=f"{parts.scheme}://{parts.netloc.lower()}")


def challenge_of(state: str) -> bytes:
    """The challenge a handshake's `state` stands for: a hash of it, so the
    state — which the cookie carries — is not the value the page shows, and the
    challenge needs no column of its own."""
    return hashlib.sha256(b"passkey challenge:" + state.encode("ascii")).digest()


# ---------------------------------------------------------------------------
# the attempt cookie
# ---------------------------------------------------------------------------

ATTEMPT_COOKIE = "tns_passkey"
ATTEMPT_COOKIE_SECURE = "__Host-tns_passkey"


def set_attempt_cookie(response: Response, state: str, settings: Settings,
                       request: Request | None = None) -> None:
    """Pin a passkey handshake to this browser — the handshake cookie's
    attributes and lifetime, under a name of its own."""
    secure = cookies.use_secure_cookie(settings, request)
    response.set_cookie(key=ATTEMPT_COOKIE_SECURE if secure else ATTEMPT_COOKIE, value=state,
                        max_age=handshakes.HANDSHAKE_TTL_SECONDS, path="/", httponly=True,
                        samesite="lax", secure=secure)


def read_attempt_cookie(request: Request, settings: Settings) -> str | None:
    secure = cookies.use_secure_cookie(settings, request)
    preferred = ATTEMPT_COOKIE_SECURE if secure else ATTEMPT_COOKIE
    other = ATTEMPT_COOKIE if secure else ATTEMPT_COOKIE_SECURE
    return request.cookies.get(preferred) or request.cookies.get(other)


def clear_attempt_cookie(response: Response, settings: Settings,
                         request: Request | None = None) -> None:
    """Drop both names once a ceremony has finished, so a spent state is never
    paired with a later attempt."""
    response.delete_cookie(ATTEMPT_COOKIE, path="/", httponly=True, samesite="lax", secure=False)
    response.delete_cookie(ATTEMPT_COOKIE_SECURE, path="/", httponly=True, samesite="lax",
                           secure=True)


async def start(purpose: str, *, session_id: str | None = None) -> str:
    """A passkey handshake: 'login' for signing in, 'link' for adding one (bound
    to `session_id`). Returns its state."""
    return await handshakes.create_handshake(provider=PROVIDER, purpose=purpose,
                                             session_id=session_id)


async def consume(request: Request, settings: Settings, *, purpose: str,
                  session_id: str | None = None) -> str:
    """The state of the handshake this browser's cookie names, claimed once.
    Raises Refused when there is none to claim — missing, expired, used, for
    another purpose, or (adding) bound to another session."""
    state = read_attempt_cookie(request, settings)
    try:
        row = await handshakes.consume_handshake(state, provider=PROVIDER, session_id=session_id)
    except handshakes.HandshakeError:
        raise Refused(REFUSED) from None
    if row["purpose"] != purpose:
        raise Refused(REFUSED)
    return str(state)


# ---------------------------------------------------------------------------
# what is stored
# ---------------------------------------------------------------------------

class Passkey(NamedTuple):
    id: int
    name: str
    created_at: int
    last_used_at: int | None
    backed_up: bool


async def for_account(user_id: int) -> list[Passkey]:
    rows = await db.fetch_all(
        "SELECT id, name, created_at, last_used_at, backed_up FROM passkeys "
        "WHERE user_id = ? ORDER BY created_at, id", (user_id,))
    return [Passkey(int(r["id"]), str(r["name"]), int(r["created_at"]), r["last_used_at"],
                    bool(r["backed_up"])) for r in rows]


async def holds_any(user_id: int) -> bool:
    """Whether the account holds a passkey: what every sign-in writes into the
    sign-in hint (cookies.set_session_cookie)."""
    return await db.fetch_value("SELECT 1 FROM passkeys WHERE user_id = ? LIMIT 1",
                                (user_id,)) is not None


def account_label(user) -> str:
    """What a device's list calls this account: the username, else the display
    name, else the instance's name — so a provider-only account still reads as
    something."""
    return (user["username"] or user["display_name"] or f"{chrome.PRODUCT_NAME} account")


async def _handle(user_id: int) -> bytes:
    """The account's passkey handle — a random id a passkey carries as its user
    id, never the row id — made with the first passkey and never again."""
    def _work(conn: db.Connection) -> str:
        conn.execute("UPDATE users SET passkey_handle = ? WHERE id = ? AND passkey_handle IS NULL",
                     (bytes_to_base64url(secrets.token_bytes(32)), user_id))
        return str(conn.execute("SELECT passkey_handle FROM users WHERE id = ?",
                                (user_id,)).fetchone()[0])
    return base64url_to_bytes(await db.transaction(_work))


# ---------------------------------------------------------------------------
# adding one
# ---------------------------------------------------------------------------

async def creation_options(rp: RelyingParty, state: str, user) -> str:
    """The options a "Create passkey" button carries, as JSON: a discoverable
    credential (signing in names no account), user verification required (the
    passkey alone signs in, so the device must check the person), no
    attestation, and none of the account's own passkeys made again."""
    user_id = int(user["id"])
    held = await db.fetch_all("SELECT credential_id, transports FROM passkeys WHERE user_id = ?",
                              (user_id,))
    label = account_label(user)
    options = generate_registration_options(
        rp_id=rp.rp_id, rp_name=chrome.PRODUCT_NAME, user_name=label, user_display_name=label,
        user_id=await _handle(user_id), challenge=challenge_of(state), timeout=PROMPT_MS,
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED),
        exclude_credentials=[PublicKeyCredentialDescriptor(
            id=base64url_to_bytes(row["credential_id"]), transports=_transports(row["transports"]))
            for row in held])
    return options_to_json(options)


def _transports(text: str) -> list[AuthenticatorTransport]:
    known = {t.value: t for t in AuthenticatorTransport}
    try:
        names = json.loads(text or "[]")
    except ValueError:
        return []
    return [known[name] for name in names if name in known]


async def finish_adding(rp: RelyingParty, state: str, user_id: int, credential: str,
                        name: str, *, now: int | None = None) -> None:
    """Verify a new passkey against the challenge `state` stands for and store
    it. Raises Refused with the sentence to say."""
    try:
        verified = verify_registration_response(
            credential=credential, expected_challenge=challenge_of(state),
            expected_rp_id=rp.rp_id, expected_origin=rp.origin, require_user_verification=True)
        transports = [str(t.value) for t in
                      (parse_registration_credential_json(credential).response.transports or [])]
    except (WebAuthnException, ValueError, KeyError, TypeError) as exc:
        logger.info("A new passkey did not verify: %s", exc)
        raise Refused("That passkey couldn't be added. Try again.") from None
    ts = db.now() if now is None else now

    def _work(conn: db.Connection) -> None:
        count = conn.execute("SELECT COUNT(*) FROM passkeys WHERE user_id = ?",
                             (user_id,)).fetchone()[0]
        if count >= MAX_PER_ACCOUNT:
            raise Refused(f"An account can hold at most {MAX_PER_ACCOUNT} passkeys. "
                          "Remove one first.")
        conn.execute(
            "INSERT INTO passkeys (user_id, credential_id, public_key, sign_count, transports, "
            "name, backed_up, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, bytes_to_base64url(verified.credential_id),
             verified.credential_public_key, int(verified.sign_count), json.dumps(transports),
             clean_name(name), 1 if verified.credential_backed_up else 0, ts))

    try:
        await db.transaction(_work)
    except db.IntegrityError:
        # The credential id is UNIQUE: this device's passkey is already here.
        raise Refused("That passkey is already on an account here.") from None


# ---------------------------------------------------------------------------
# signing in
# ---------------------------------------------------------------------------

def sign_in_options(rp: RelyingParty, state: str) -> str:
    """The options a "Sign in with a passkey" button and the username box's
    autofill carry, as JSON: no account named (the passkey is discoverable) and
    user verification required."""
    return options_to_json(generate_authentication_options(
        rp_id=rp.rp_id, challenge=challenge_of(state), timeout=PROMPT_MS,
        user_verification=UserVerificationRequirement.REQUIRED))


async def finish_sign_in(rp: RelyingParty, state: str, credential: str, *,
                         now: int | None = None) -> int:
    """The account a passkey's answer signs in, or Refused with `REFUSED`.

    The passkey is found by its credential id, and the answer's user handle must
    be that account's own; the signature is checked against the challenge
    `state` stands for, this instance's origin and rp id, with user verification
    required. A sign count that goes backwards is refused (a synced passkey
    reports 0 throughout, which is accepted). A disabled account is refused."""
    try:
        parsed = parse_authentication_credential_json(credential)
    except (WebAuthnException, ValueError, KeyError, TypeError):
        raise Refused(REFUSED) from None
    row = await db.fetch_one(
        "SELECT p.id, p.user_id, p.public_key, p.sign_count, u.passkey_handle, u.is_disabled "
        "FROM passkeys p JOIN users u ON u.id = p.user_id WHERE p.credential_id = ?",
        (bytes_to_base64url(parsed.raw_id),))
    if row is None or row["is_disabled"] or not row["passkey_handle"]:
        raise Refused(REFUSED)
    handle = parsed.response.user_handle
    if not handle or not secrets.compare_digest(handle, base64url_to_bytes(row["passkey_handle"])):
        raise Refused(REFUSED)
    try:
        verified = verify_authentication_response(
            credential=parsed, expected_challenge=challenge_of(state), expected_rp_id=rp.rp_id,
            expected_origin=rp.origin, credential_public_key=bytes(row["public_key"]),
            credential_current_sign_count=int(row["sign_count"]), require_user_verification=True)
    except (WebAuthnException, ValueError, KeyError, TypeError) as exc:
        logger.info("A passkey sign-in did not verify: %s", exc)
        raise Refused(REFUSED) from None
    await db.execute(
        "UPDATE passkeys SET sign_count = ?, backed_up = ?, last_used_at = ? WHERE id = ?",
        (int(verified.new_sign_count), 1 if verified.credential_backed_up else 0,
         db.now() if now is None else now, int(row["id"])))
    return int(row["user_id"])


# ---------------------------------------------------------------------------
# naming and removing
# ---------------------------------------------------------------------------

def clean_name(name: Any) -> str:
    """A passkey's name as stored: whitespace collapsed, at most NAME_MAX
    characters, and "Passkey" where nothing is left."""
    text = re.sub(r"\s+", " ", str(name or "")).strip()[:NAME_MAX].strip()
    return text or "Passkey"


async def rename(user_id: int, passkey_id: int, name: Any) -> bool:
    """Rename one of the account's passkeys. False when it holds no such one."""
    def _work(conn: db.Connection) -> bool:
        return conn.execute("UPDATE passkeys SET name = ? WHERE id = ? AND user_id = ?",
                            (clean_name(name), passkey_id, user_id)).rowcount == 1
    return await db.transaction(_work)


async def remove(user_id: int, passkey_id: int, *, force: bool = False) -> bool:
    """Remove one of the account's passkeys. False when it holds no such one.

    Raises identities.LastLoginMethod when it is the account's last way in —
    asked of identities.ways_in, in the same transaction, as an unlink is. Only
    an administrator passes `force`, after a confirmation."""
    def _work(conn: db.Connection) -> bool:
        if conn.execute("SELECT 1 FROM passkeys WHERE id = ? AND user_id = ?",
                        (passkey_id, user_id)).fetchone() is None:
            return False
        if identities.ways_in(conn, user_id).leaves_none(passkey=passkey_id) and not force:
            raise identities.LastLoginMethod()
        conn.execute("DELETE FROM passkeys WHERE id = ?", (passkey_id,))
        return True
    return await db.transaction(_work)


# ---------------------------------------------------------------------------
# a name for a new one
# ---------------------------------------------------------------------------

# Read in order: Edge and Opera say "Chrome" too, and Chrome says "Safari".
_BROWSERS = (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"),
             ("Chrome/", "Chrome"), ("Safari/", "Safari"))
_SYSTEMS = (("Android", "Android"), ("iPhone", "iPhone"), ("iPad", "iPad"),
            ("Windows", "Windows"), ("CrOS", "ChromeOS"), ("Mac OS X", "macOS"),
            ("Linux", "Linux"))


def suggested_name(user_agent: str | None) -> str:
    """"Firefox on Windows", from the request that is adding a passkey — a name
    the person can keep or change, so a list of them can be told apart."""
    agent = user_agent or ""
    browser = next((name for mark, name in _BROWSERS if mark in agent), "")
    system = next((name for mark, name in _SYSTEMS if mark in agent), "")
    if browser and system:
        return f"{browser} on {system}"
    return browser or system or "Passkey"
