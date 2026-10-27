"""A picture that will not load says so, and its title is described again.

A RECORDED ADDRESS CAN DIE AT ITS HOST AND NOTHING TELLS THE SERVER: a service
replaces a poster, re-points an id, and the address the registry holds answers
404 for ever. Checking every picture to find out would be a request per picture
per page. Instead a signed-in page draws its first picture through the image
proxy with THIS APP'S OWN REPORT ADDRESS as the proxy's `default` (`drawn`): a
picture that loads costs nothing, and one that does not sends the browser here
— the proxy redirects to its default on every failure, with no script.

THE ANSWER IS AT ONCE (`answer`): the first picture the registry holds for the
title, in the viewer's order (artwork.ranked's — the same answer every surface
draws), that this chain of hops has not already tried, itself carrying its own
report address — or the placeholder when nothing is left. Which picture comes
next is the server's decision, and it is made against what the registry holds
NOW: a service that filed a better picture since the page was drawn (its
description arrived behind the page) is offered first. The token carries every
address the chain has tried, so two dead pictures can never send a browser back
and forth between them.

THE CONFIRMATION IS BEHIND IT (`confirm`), because a report alone is not proof:
the proxy falls back on every failure — a 404, a 200 that is not an image, an
unresolvable host, a 5xx, a 403 (measured) — and only some of those say the
picture is gone. So the ONE address the report names is read back from the
registry, never taken from the request, and asked with a HEAD:
  - 404, 410, or a body that is not an image: DEAD. The row goes to the cap, so
    nothing draws it again, and the WHOLE TITLE is described again by every
    service that knows it, at the front of the refresh queue (`heal`). A dead
    picture is a sign the title itself moved — a replaced poster, a re-pointed
    or re-keyed id — so every service's row is rewritten and its ids go through
    the one resolution, which re-keys the title if a service now names it
    differently. The refreshed rows file the services' current addresses (a
    CHANGED address starts with no failures; the same dead one stays capped).
  - 5xx, 403, a timeout: TRANSIENT. When, and nothing else; no refresh.
  - Anything an image: ALIVE, and nothing changes.
Bounded three ways: a report is checked at most once per address in
`DEDUPE_SECONDS`, at most `CONFIRMS_PER_MINUTE` a minute, and a title is healed
at most once in `HEAL_EVERY_SECONDS` however many of its pictures report.

THE ROUTE IS PUBLIC (app/metadata/picture_routes.py), because the hop arrives
from the proxy's redirect and Firefox does not send the SameSite=Lax session
cookie on it (measured). What it may act on is decided by the TOKEN, signed here
and short-lived: the title, the service and season whose picture was drawn, a
digest of the address, the viewer's order and when it was issued. A stranger
cannot condemn a picture the server did not draw — the digest must match what
the registry holds — nor replay an old page for ever. The signing key is derived
from the instance's ENCRYPTION_KEY (secrets_box.derived_key — a key of its own,
never the one that seals the secrets), so a token survives a restart. An
instance that has not turned at-rest encryption on has no secret to derive one
from, and signs with a key made when the process starts: a page drawn before a
restart then gets the placeholder for a picture that fails once.

A SHARE PAGE NEVER REPORTS: it draws the static fallback chain
(providers.base.proxied_image's `fallbacks`), and a stranger's view of it costs
the owner nothing. Neither does an instance with no public address configured,
which has no absolute address to hand the proxy.

EVERY STEP IS LOGGED AT INFO, one line each, naming the title's key, the service
and a shortened address (host and last path segment, never a token or a whole
signed address), so a heal can be followed end to end by its key.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from collections import deque
from collections.abc import Iterable, Sequence
from typing import NamedTuple
from urllib.parse import urlsplit

import httpx

from .. import db, http_pool, secrets_box
from ..media import artwork
from ..providers.base import proxied_image, proxied_origin
from . import identity, refresh

logger = logging.getLogger(__name__)

ROUTE = "/api/posters/missed"
PLACEHOLDER = "/static/images/nopostertv.png"

# How long a drawn page's report addresses are honoured: a working day of a tab
# left open, and not a replay for ever.
TOKEN_SECONDS = 6 * 60 * 60
# One check per address per this long, however many rows and tabs report it —
# every view of a dead picture reports it again, and nothing caches the hop.
DEDUPE_SECONDS = 10 * 60
# And at most this many checks a minute, whatever arrives.
CONFIRMS_PER_MINUTE = 30
# One heal per title per this long, however many of its pictures report.
HEAL_EVERY_SECONDS = 6 * 60 * 60

# What the signing key is derived for (secrets_box.derived_key).
_PURPOSE = "picture report token"
# The key an instance with no ENCRYPTION_KEY signs with, made when the process
# starts and never stored.
_PROCESS_KEY = secrets.token_bytes(32)


def _signing_key() -> bytes:
    return secrets_box.derived_key(_PURPOSE) or _PROCESS_KEY

# The confirmation's own pool: a HEAD of an image host, no credential — a token
# sent to an address recorded from somebody else would hand them one.
POOL = http_pool.Pool("picture-check", max_connections=4, timeout=10)


class Title(NamedTuple):
    """The title a picture was drawn for: its tmdb id, or — for a title with none
    — the title store's key for it, and the season a row draws (None: the title)."""
    media: str
    tmdb: int | None
    title_key: str
    season: int | None

    @property
    def key(self) -> str:
        """The title store's key, which every log line names."""
        return f"{self.media}:tmdb:{self.tmdb}" if self.tmdb else self.title_key


class Report(NamedTuple):
    """What a report address says: the title, which service's picture was drawn
    and whether it was the season's own (`season`), a digest of its address, the
    viewer's order, when the page was drawn, and the digests of every address
    this chain of hops has tried, the reported one last (`tried`)."""
    title: Title
    source: str
    season: int | None
    digest: str
    order: tuple[str, ...]
    issued_at: int
    tried: tuple[str, ...] = ()


def digest(url: str) -> str:
    """An address as a token names it: enough to tell it from any other, and no
    address in the page twice."""
    return hashlib.sha256(str(url).encode("utf-8")).hexdigest()[:20]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def token(report: Report) -> str:
    """A report signed with this process's key."""
    t = report.title
    payload = _b64(json.dumps(
        [t.media, t.tmdb, t.title_key, t.season, report.source, report.season, report.digest,
         list(report.order), report.issued_at, list(report.tried or (report.digest,))],
        separators=(",", ":")).encode("utf-8"))
    signature = _b64(hmac.new(_signing_key(), payload.encode("ascii"),
                              hashlib.sha256).digest()[:16])
    return f"{payload}.{signature}"


def read(text: str, *, now: int) -> Report | None:
    """The report a token says, or None for one this process did not sign, one
    past `TOKEN_SECONDS`, or anything malformed."""
    try:
        payload, signature = str(text or "").split(".", 1)
        expected = _b64(hmac.new(_signing_key(), payload.encode("ascii"),
                                 hashlib.sha256).digest()[:16])
        if not hmac.compare_digest(signature, expected):
            return None
        (media, tmdb, title_key, season, source, own, address, order, issued,
         tried) = json.loads(_unb64(payload))
        report = Report(Title(str(media), int(tmdb) if tmdb else None, str(title_key or ""),
                              None if season is None else int(season)),
                        str(source), None if own is None else int(own), str(address),
                        tuple(str(name) for name in order), int(issued),
                        tuple(str(found) for found in tried))
    except (ValueError, TypeError, UnicodeError):
        return None
    if not 0 <= now - report.issued_at <= TOKEN_SECONDS:
        return None
    return report


def _origin(settings) -> str:
    """This instance's own origin as configured, or "" — a report address must
    be absolute for the proxy to send a browser to it."""
    parts = urlsplit((getattr(settings, "public_base_url", "") or "").strip())
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""


def report_address(settings, title: Title, choice: artwork.Choice, order: Iterable[str], *,
                   now: int | None = None, tried: Iterable[str] = ()) -> str:
    """This app's report address for one drawn picture, or "" without a
    configured public address. `tried` is what the chain of hops that led here
    already tried (`answer`); this picture is added to it."""
    origin = _origin(settings)
    if not origin:
        return ""
    address = digest(choice.url)
    report = Report(title, str(choice.source), choice.season, address,
                    tuple(str(name) for name in order), db.now() if now is None else now,
                    (*tried, address))
    return f"{origin}{ROUTE}?t={token(report)}"


def drawn(settings, title: Title, choices: Sequence[artwork.Choice], order: Iterable[str],
          *, now: int | None = None, tried: Iterable[str] = ()) -> str:
    """What a SIGNED-IN page draws for a title's picture: the first of `choices`
    (artwork's ranked order) through the image proxy, with its report address as
    the proxy's default. "" when there is no picture. Without a configured public
    address, the static fallback chain instead."""
    if not choices:
        return ""
    first = choices[0]
    report = report_address(settings, title, first, order, now=now, tried=tried)
    if not report:
        return proxied_image(first.url, fallbacks=[choice.url for choice in choices[1:]]) or ""
    return proxied_image(first.url, report=report) or ""


async def next_choice(report: Report) -> artwork.Choice | None:
    """The first picture, in the report's order, that the chain has not tried,
    from what the registry holds now — the server's decision, never the
    request's — or None."""
    t = report.title
    known = await artwork.known([(t.media, t.tmdb, t.season)] if t.tmdb else [],
                                [] if t.tmdb else [(t.media, t.title_key)])
    choices = known.choices(t.media, t.tmdb, t.season, report.order, title_key=t.title_key)
    tried = set(report.tried or (report.digest,))
    return next((choice for choice in choices if digest(choice.url) not in tried), None)


async def answer(settings, text: str, *, now: int | None = None
                 ) -> tuple[str, Report | None]:
    """Where to send a browser whose picture did not load, and the report to
    confirm behind the answer (None for a refused one)."""
    ts = db.now() if now is None else now
    report = read(text, now=ts)
    if report is None:
        logger.info("pictures: report refused — its token is not this instance's, or "
                    "has expired")
        return PLACEHOLDER, None
    found = await next_choice(report)
    t = report.title
    # The reported address, named in the log as the registry holds it — the
    # request only ever carries its digest.
    stored = await artwork.address_of(t.media, t.tmdb, t.title_key, report.season,
                                      report.source)
    reported = (refresh.shortened(stored[0])
                if stored is not None and digest(stored[0]) == report.digest
                else "(an address since replaced)")
    if found is None:
        logger.info("pictures: report received — %s's %s picture %s did not load; nothing "
                    "left to draw, the placeholder instead", t.key, report.source, reported)
        return PLACEHOLDER, report
    logger.info("pictures: report received — %s's %s picture %s did not load; drawing "
                "%s's %s next", t.key, report.source, reported, found.source,
                refresh.shortened(found.url))
    return (drawn(settings, t, [found], report.order, now=ts,
                  tried=report.tried or (report.digest,)), report)


# ---------------------------------------------------------------------------
# behind the answer
# ---------------------------------------------------------------------------

_checked: dict[str, float] = {}
_checks: deque[float] = deque()
_healed: dict[str, float] = {}


async def confirm(settings, report: Report) -> str:
    """Check the ONE address a report names and act on what it is — see the
    module docstring. Returns what was found: "dead", "transient", "alive",
    "replaced" (the registry holds another address now), "repeat" or "limited"."""
    t = report.title
    moment = time.monotonic()
    last = _checked.get(report.digest)
    if last is not None and moment - last < DEDUPE_SECONDS:
        logger.debug("pictures: %s's %s picture was reported again within %d minutes; "
                     "checked once", t.key, report.source, DEDUPE_SECONDS // 60)
        return "repeat"
    while _checks and moment - _checks[0] >= 60:
        _checks.popleft()
    if len(_checks) >= CONFIRMS_PER_MINUTE:
        logger.info("pictures: report of %s's %s picture refused — %d checks this minute "
                    "already", t.key, report.source, CONFIRMS_PER_MINUTE)
        return "limited"
    _checks.append(moment)
    _checked[report.digest] = moment
    for stale in [d for d, at in _checked.items() if moment - at >= DEDUPE_SECONDS]:
        del _checked[stale]
    stored = await artwork.address_of(t.media, t.tmdb, t.title_key, report.season,
                                      report.source)
    if stored is None or digest(stored[0]) != report.digest:
        logger.info("pictures: %s's %s picture was replaced since the page drew it; "
                    "nothing to check", t.key, report.source)
        return "replaced"
    url = stored[0]
    outcome, status, millis = await _check(url)
    logger.info("pictures: %s's %s picture %s is %s (HEAD %s, %d ms)", t.key, report.source,
                refresh.shortened(url), outcome.upper(), status, millis)
    if outcome == "dead":
        if await artwork.cap(t.media, t.tmdb, t.title_key, report.season, report.source, url):
            logger.info("pictures: capped %s's %s picture %s — nothing draws it again",
                        t.key, report.source, refresh.shortened(url))
        await heal(settings, report)
    elif outcome == "transient":
        await artwork.note_failure(t.media, t.tmdb, t.title_key, report.season,
                                   report.source, url)
    return outcome


async def _check(url: str) -> tuple[str, str, int]:
    """(dead | transient | alive, the status as logged, milliseconds) for one
    address."""
    started = time.perf_counter()
    try:
        response = await POOL.client().head(url, follow_redirects=True)
    except httpx.HTTPError as exc:
        return "transient", type(exc).__name__, int((time.perf_counter() - started) * 1000)
    millis = int((time.perf_counter() - started) * 1000)
    status = response.status_code
    kind = (response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if status in (404, 410):
        return "dead", str(status), millis
    if 200 <= status < 300:
        # A body that is not an image is a page where a picture was: dead. A host
        # that does not say what it is serving is given the benefit of the doubt.
        return ("dead" if kind and not kind.startswith("image/") else "alive",
                f"{status} {kind or 'untyped'}", millis)
    return "transient", str(status), millis


async def heal(settings, report: Report) -> list:
    """Describe the whole title again, from every service that knows it — its
    title row, and the season the picture was drawn for — at the front of the
    refresh queue, and run the pass. At most once per title per
    `HEAL_EVERY_SECONDS`. Returns what was queued."""
    t = report.title
    moment = time.monotonic()
    last = _healed.get(t.key)
    if last is not None and moment - last < HEAL_EVERY_SECONDS:
        logger.info("pictures: %s was healed %d minutes ago; not again yet", t.key,
                    int((moment - last) // 60))
        return []
    _healed[t.key] = moment
    found = await identity.known(t.key)
    ids = dict(found.ids) if found is not None else ({"tmdb": t.tmdb} if t.tmdb else {})
    media = found.media if found is not None else t.media
    seasons = [t.season] if t.season is not None else []
    entries = await refresh.queue_title(settings, media, ids, seasons,
                                        reason=refresh.REASON_DEAD_PICTURE, title_key=t.key)
    logger.info("pictures: heal of %s queued — %s (%s)", t.key,
                ", ".join(f"{entry.source} "
                          + ("title" if entry.season == refresh.TITLE
                             else f"season {entry.season}") for entry in entries)
                or "no service this instance can ask", refresh.REASON_DEAD_PICTURE)
    if entries:
        await refresh.run(settings)
    return entries


class CardPictures:
    """What a SIGNED-IN calendar draws for a card's picture: the address the card
    was rendered with, re-addressed with this app's report address behind it.
    Handed to the card template by the routes that draw a viewer's own cards
    (app/calendar/routes.py's `_card_pictures`) — a share page never is, and
    keeps the static fallback chain render() drew.

    The picture is the card's own, the service whose value the card shows
    (Item.shown_source), at the title's level: a card's picture is its show's,
    whichever episode it is."""

    def __init__(self, settings, order: Iterable[str], *, now: int | None = None):
        self.settings = settings
        self.order = tuple(str(name) for name in order)
        self.now = db.now() if now is None else now

    def __call__(self, item, source: str, value: str | None) -> str:
        return (self.picture(item.media, item.ids, item.mark_key, source,
                             proxied_origin(value)) or str(value or ""))

    def picture(self, media, ids, title_key: str, source: str, origin: str) -> str:
        """One title's picture from one service, by its ORIGIN address — a
        card's, or the card's panel's — or "" when there is none."""
        if not origin or not source:
            return ""
        try:
            tmdb = int((ids or {}).get("tmdb") or 0) or None
        except (TypeError, ValueError):
            tmdb = None
        title = Title(str(media), tmdb, "" if tmdb else str(title_key or ""), None)
        return drawn(self.settings, title, [artwork.Choice(str(source), str(origin), None)],
                     self.order, now=self.now)
