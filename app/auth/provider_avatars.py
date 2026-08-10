"""Fetching the profile picture a provider hands back, safely.

WHY THIS IS ITS OWN MODULE AND WHY IT IS THE CAREFUL ONE. Everything else in
this feature moves bytes between the app's own directories. This is the single
place that takes a URL out of a THIRD PARTY'S JSON and makes the server go and
ask for it, which is a request an attacker chooses. `169.254.169.254` (the cloud
metadata endpoint) and `localhost` are both reachable from inside the container
this runs in, so a URL fetched without checking where it points is a
server-side request forgery with the app's own network position behind it.

THE RULE, AND IT HAS NO EXCEPTIONS:

  1. https ONLY. Not http, not a scheme-relative "//host/path", not `file:`.
  2. THE HOST MUST BE IN THIS PROVIDER'S OWN LITERAL ALLOWLIST — compared whole,
     lower-cased, never by suffix. A suffix test (`endswith("plex.tv")`) is
     defeated by `evilplex.tv` and by `plex.tv.attacker.com`, which is exactly
     the shape of bug this list exists to prevent.
  3. NO REDIRECTS FOLLOWED. A redirect relocates the request to a host the
     allowlist never saw, which would hand rule 2 straight back.
  4. THE SIZE CAP IS ENFORCED WHILE READING, not from Content-Length — a header
     is a claim, and a body that keeps coming is what actually fills memory.
  5. ANY FAILURE IS A NON-EVENT. No avatar is the ordinary case; this never
     raises to its caller and never delays a sign-in past its own short timeout.

THE HOSTS WERE MEASURED, NOT GUESSED, against all three providers' live account
payloads on 2026-08-09, because a wrong allowlist fails silently in both
directions — too narrow and every avatar is dropped while looking like the
feature simply does nothing, too broad and the list is the vulnerability.

    trakt   user.images.avatar.full   -> media.trakt.tv
    plex    thumb                     -> plex.tv
    simkl   user.avatar               -> depends, and that is the interesting one

SIMKL SERVES TWO DIFFERENT HOSTS DEPENDING ON WHERE THE PICTURE CAME FROM, which
was established by measuring the SAME account twice, before and after uploading a
picture to Simkl rather than keeping the one it imported at sign-up:

    an UPLOADED avatar   -> simkl.in        (https://simkl.in/avatars/88/8814058_100.jpg)
    an IMPORTED avatar   -> the social provider's own CDN, e.g.
                            lh3.googleusercontent.com for a Google sign-up

Only the first is on the allowlist. `simkl.in` is a literal host this app already
talks to — the calendar's posters come from `simkl.in/posters/` — so it is
exactly the kind of small, known entry this list is for. The second is not a host
Simkl owns at all but whichever identity provider that person happened to use, so
covering it would mean listing every social CDN in existence, which is not a
compromise but the vulnerability with extra steps.

WHAT THAT MEANS IN PRACTICE, AND IT IS THE HONEST OUTCOME RATHER THAN A GAP: a
Simkl account whose picture Simkl actually hosts is seeded, and one still showing
an imported social picture is not. The second is a silent no-op, which is the
same thing that already happens for an account with no avatar at all — see rule 5
— so nothing new has to be explained to anybody. It also resolves itself the
moment that person uploads a picture to Simkl, with no change here.
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx

from .. import http_pool

logger = logging.getLogger(__name__)

# One pool, small and short-timeout: this runs inside a sign-in that has already
# spent a token exchange and an account lookup, and a slow image CDN must never
# be what holds that open. See TIMEOUT_SECONDS.
POOL = http_pool.Pool("provider-avatars", max_connections=2, timeout=10)

TIMEOUT_SECONDS = 6.0

# Matched to app/media/user_images.py's own upload ceiling: a provider's picture
# goes through exactly the same validation an upload does (see that module's
# save_provider_avatar), so accepting a larger one here would only mean spending
# the bytes before the validator refused them.
MAX_BYTES = 5 * 1024 * 1024

# Per provider, whole-host, lower-case. Every entry here was measured against a
# live account (see the module docstring); none was inferred from a domain name.
# An empty tuple would mean "this provider is not seeded at all", which is a
# decision rather than an omission — no provider is in that state today.
ALLOWED_HOSTS: dict[str, tuple[str, ...]] = {
    "trakt": ("media.trakt.tv",),
    "plex": ("plex.tv",),
    # NOT the social CDNs Simkl also hands back for an imported picture. See the
    # module docstring: this covers an avatar Simkl itself hosts and nothing else.
    "simkl": ("simkl.in",),
}


def allowed_url(provider: str, url: str | None) -> str | None:
    """`url` if this provider is allowed to be fetched from that host, else None.

    PURE, AND SEPARATE FROM THE FETCH ON PURPOSE. "Is this URL allowed" is the
    whole security decision, so it is a function that can be exhaustively tested
    with no network, no mock and no I/O — the fetch below is then only plumbing.
    """
    if not url or not isinstance(url, str):
        return None
    hosts = ALLOWED_HOSTS.get(provider)
    if not hosts:
        return None
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    # `scheme` is compared exactly: a scheme-relative "//host/x" parses with an
    # empty scheme and would otherwise sail through a truthiness check.
    if parsed.scheme != "https":
        return None
    # `hostname` and not `netloc`: netloc carries any userinfo and port, so
    # "media.trakt.tv@evil.example" is a netloc whose HOST is evil.example.
    # hostname is the part the request actually connects to, already lower-cased.
    if parsed.hostname is None or parsed.hostname not in hosts:
        return None
    return url.strip()


async def current_url(user_id: int, provider: str) -> str | None:
    """Ask the service, using this account's stored token, where its picture is
    now. None when there is no link, no usable token, or the lookup fails.

    THE ONLY CALLER IS THE EXPLICIT REFRESH BUTTON. Sign-in already has a fresh
    account payload in hand and passes the URL straight through, so this exists
    for the one path that has no payload and must go and get one — which is why
    it is worth an account lookup and why an ordinary sign-in is not.

    Each provider's `fetch_account` takes its arguments in its own order, which
    is why this dispatches explicitly rather than looping: they are three
    different APIs that happen to answer the same question.
    """
    from . import identities, plex, simkl, trakt  # deferred: they import this module's siblings
    from .. import secrets_box
    from ..config import load_settings

    if provider not in ALLOWED_HOSTS:
        return None
    for row in await identities.list_identities(user_id):
        if row["provider"] != provider:
            continue
        # Sealed at rest. `open_` returns None when the at-rest key is missing,
        # which is the same answer as "no token" here: no picture, no refusal.
        token = secrets_box.open_(row["access_token"])
        if not token:
            return None
        settings = load_settings()
        try:
            if provider == "trakt":
                account = await trakt.fetch_account(settings.trakt_client_id, token)
            elif provider == "simkl":
                account = await simkl.fetch_account(settings.simkl_client_id, token)
            else:
                account = await plex.fetch_account(token, await plex.ensure_client_identifier())
        except Exception as exc:  # noqa: BLE001 — every lookup failure is "no picture"
            logger.debug("Could not re-read the %s account for user %s: %s",
                         provider, user_id, exc)
            return None
        return account.get("avatar")
    return None


async def fetch(provider: str, url: str | None) -> bytes | None:
    """The image bytes at `url`, or None for any reason at all.

    Never raises. A provider with no avatar, a URL pointing somewhere this app
    will not go, a timeout, a 404, a body over the cap — all of them are the
    same answer here, because all of them mean the same thing to the caller:
    there is no picture to seed and the sign-in carries on regardless.
    """
    target = allowed_url(provider, url)
    if target is None:
        return None
    try:
        client = POOL.client()
        # follow_redirects is FALSE and is stated rather than left to the
        # client's default, because this is the one call in the app where the
        # default changing would silently undo a security rule.
        async with client.stream("GET", target, timeout=TIMEOUT_SECONDS,
                                 follow_redirects=False) as response:
            if response.status_code != 200:
                return None
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                # ABANDONED MID-READ. The connection is dropped by leaving the
                # stream context, so a body that keeps coming costs this much
                # and no more, whatever Content-Length claimed.
                if total > MAX_BYTES:
                    logger.debug("%s avatar exceeded %d bytes; abandoned.", provider, MAX_BYTES)
                    return None
                chunks.append(chunk)
    except (httpx.HTTPError, ValueError, OSError) as exc:
        # DEBUG, not WARNING: a provider picture that did not arrive is not a
        # fault anybody needs to act on, and this runs on every registration.
        logger.debug("Could not fetch the %s avatar: %s", provider, exc)
        return None
    return b"".join(chunks) or None
