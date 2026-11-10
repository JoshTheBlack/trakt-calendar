"""Trakt's calendar endpoints, and the normalizer that turns what they return
into the uniform `Record` every calendar source produces.

Fetching and normalizing are kept together here because they are two halves of
one answer to one question — "what airs in this window" — but to_record() itself
is pure: it takes a raw entry and returns a Record, so it can be exercised
against a captured payload with nothing else running.

NORMALIZING HAPPENS HERE, ONCE, ON THE WAY IN. The calendar cache stores Records
and knows nothing about Trakt's field layout; that is what lets a second source
fill the same cache instead of the cache growing a branch per source.

NOTHING IN THIS MODULE FILTERS. Filtering is the calendar feature's job and it
happens on the far side of the cache, which is what keeps this package free of
any import back into app/calendar/filter.py — the one edge that used to make
the calendar and the provider mutually dependent.
"""
from __future__ import annotations

import logging
import time as _time
from datetime import date, datetime, timedelta
from urllib.parse import quote, urlencode

import httpx

from ... import perftrace
from ...config import Settings
from ...endpoints import Endpoint
from ..base import epoch_seconds, Media, Record, Source
from . import _ids, transport
from .transport import TraktError

logger = logging.getLogger(__name__)
_perf = logging.getLogger("app.perf")


def calendar_path(endpoint: Endpoint) -> str:
    """This endpoint's Trakt path segment, the part after /calendars/all/.

    THE TRANSLATION FROM THE APP'S ENDPOINT KEY TO TRAKT'S OWN URL, and the only
    place it happens. The keys and the paths coincide today, which is precisely
    why this needs to be a function and not an assumption: another source spells
    the same five calendars completely differently, and a caller that formatted
    `endpoint.key` into a URL would look correct right up until it was asked to
    do it for somebody else.
    """
    return endpoint.key


async def fetch_window(endpoint: Endpoint, settings: Settings, start: date, days: int) -> list[Record]:
    """One window of Trakt's calendar, as Records.

    THE ONLY PLACE THIS APP ASKS TRAKT WHAT AIRS. Everything downstream — the
    calendar cache's window fill, and through it every month a viewer sees — is
    this plus what that caller does with the answer. It was two copies until they
    began to drift, which is what a second copy looks like: the same thirty
    lines, and two different wordings of the same pagination warning.

    Returns NORMALIZED but UNFILTERED records, in the order Trakt sent them. An
    entry this normalizer cannot read — no media object, no air date, an
    unparseable one — is dropped rather than raised over: a window is a list
    somebody else assembled and one unusable row in it is not a reason to lose
    the other four hundred.

    Filtering is not declined here for want of somewhere to put it: the calendar
    cache stores the unfiltered window once and applies the instance-wide content
    floor before storage and the per-viewer genre/country/network spec at read
    time, so a filter applied here would either be the wrong one or would have to
    be re-applied anyway.

    genres/countries are NOT sent as query params. The calendar cache stores the
    complete unfiltered result so those can be read-time per-viewer filters —
    one viewer including JP/KR shows and another excluding them from the same
    cached data — and the equivalent filtering is reproduced client-side via
    app/calendar/filter.py.

    Pagination headers are not sent either: Trakt's calendar endpoints ignore
    them and return the whole window in one response (verified live). The warning
    below fires if that ever changes, because the symptom would otherwise be a
    silently short window.

    Raises TraktError for a refusal, a non-200, a body that will not parse, or a
    Trakt that could not be reached at all (see transport.send_or_unavailable). A
    body that parses to something other than a list reads as an empty window —
    there is nothing to salvage from it and nothing to report about it.

    A WINDOW TRAKT COULD NOT BUILD IS ASKED FOR AGAIN A DAY AT A TIME. Trakt
    builds a calendar answer on demand, and an All Episodes week is up to 1.3 MB;
    MEASURED 2026-10-23, while Trakt was slow its origin answered HTTP 500 after
    five to ten seconds for whole weeks that a later try then built in under four.
    A one-day request is about a quarter of the size (330–380 KB measured — Trakt
    hands back several days for one, see the cache's `in_window`), so seven of
    them are seven smaller builds rather than one large one, and each that
    succeeds is cached by Trakt for an hour (transport.api_headers), so a retry
    rebuilds only the days that failed. ALL SEVEN OR THE WINDOW FAILS: a fill
    deletes the rows of a source that answered, so a week stored from five of its
    seven days would lose the other two days' airings rather than keep them.
    """
    try:
        return await _fetch_days(endpoint, settings, start, days)
    except TraktError as exc:
        if days <= 1 or not _trakt_could_not_build(exc):
            raise
        logger.info("Trakt could not build the %s window starting %s (%s); asking for "
                    "it a day at a time.", endpoint.key, start, exc)
    records: list[Record] = []
    for offset in range(days):
        records.extend(await _fetch_days(endpoint, settings,
                                         start + timedelta(days=offset), 1))
    return records


def _trakt_could_not_build(exc: TraktError) -> bool:
    """Whether a failure is Trakt failing to BUILD an answer — a 5xx, or our own
    request giving up on a slow one — rather than refusing the request (a 4xx, a
    rate limit) or not being reachable at all, where asking seven more times
    would only fail seven more times."""
    if isinstance(exc, transport.TraktRateLimitError):
        return False
    if exc.status is not None:
        return exc.status >= 500
    return isinstance(exc.__cause__, httpx.TimeoutException)


async def _fetch_days(endpoint: Endpoint, settings: Settings, start: date,
                      days: int) -> list[Record]:
    """ONE request for `days` days of Trakt's calendar from `start`, as Records —
    the request `fetch_window` makes, once for a window or once per day of it."""
    url = (
        f"{transport.API_BASE}/calendars/all/{calendar_path(endpoint)}/{start.isoformat()}/{days}"
        f"?{urlencode({'extended': 'full,images'})}"
    )
    t0 = _time.perf_counter()
    # WITHOUT THE ACCOUNT'S TOKEN: every calendar is public, and the token is
    # what stops Trakt caching the answer (transport.api_headers measures it).
    resp = await transport.send_or_unavailable(
        transport.shared_client(), "GET", url,
        headers=transport.api_headers(settings, paginate=False, account=False))
    _perf.debug("netGET    calendar/%s/%s+%dd -> %s  %.0fms%s", endpoint.key, start.isoformat(),
                days, resp.status_code, (_time.perf_counter() - t0) * 1000.0,
                perftrace.activity_tag())
    if resp.status_code == 401:
        raise TraktError("Trakt rejected the credentials (401). Check Client ID / Access Token in Settings.", 401)
    if resp.status_code != 200:
        raise TraktError(f"Trakt API returned HTTP {resp.status_code}.", resp.status_code)
    if resp.headers.get("x-pagination-page-count"):
        logger.warning(
            "Trakt calendar response carried pagination headers (page-count=%s) for %s; "
            "the window may be truncated.",
            resp.headers.get("x-pagination-page-count"), url,
        )
    try:
        raw = resp.json()
    except ValueError:
        raise TraktError("Trakt API returned an unreadable response.")
    if not isinstance(raw, list):
        return []
    records = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        record = to_record(entry, endpoint.media)
        if record is not None:
            records.append(record)
    return records


def poster(media: dict) -> str | None:
    """Trakt's poster for a title, as Trakt's own address.

    THE ORIGIN, NOT THE PROXY. Trakt asks that its images not be hotlinked, and
    this app honours that in `render` — the one place a stored record becomes a
    browser-facing one. Proxying here instead would also proxy the copy
    app/media/artwork.py files in the poster registry, which exists so
    app/media/posters.py can DOWNLOAD it server-side; that download is what Trakt
    asks for, and routing it through a third party would neither help them nor
    this app. Trakt publishes these paths without a scheme, which is why the
    prefix is added rather than assumed.
    """
    imgs = media.get("images") or {}
    posters = imgs.get("poster") or []
    if posters:
        url = posters[0]
        if not url.startswith("http"):
            url = "https://" + url
        return url
    return None


def listing_id(ids: dict) -> str:
    """The id a Trakt listing of a title is filed under: its slug where Trakt
    names one, its number otherwise — readable in a log line and an address,
    and Trakt resolves either. A payload names the slug `slug`; a normalized id
    map, which is what the title store keeps, `trakt_slug` (`_ids.normalize`),
    so a stored title's ids give the same answer as its listing did."""
    return str(ids.get("slug") or ids.get("trakt_slug") or ids.get("trakt") or "")


def title_url(media: Media, ids: dict) -> str:
    """The canonical Trakt page for a title, named the way Trakt prefers.

    PUBLIC, AND ANSWERED FOR AN ID MAP FROM ANYWHERE. A calendar record is not
    the only thing that has to link back here: the details panel draws an
    outbound button per service, from the union of ids a card carried, and both
    services ask that their data be linked to the page for the specific item
    rather than to a homepage. One reading of "where does this title live" per
    service, called by whoever needs it.

    THE NAMESPACED SLUG, NEVER THE SHARED ONE. Trakt and Simkl both call a
    title's readable name `slug` and disagree about it, so in a map holding both
    services' ids the bare `slug` is whichever wrote last — handing that to this
    function builds a Trakt URL for a name only Simkl uses. `normalize` writes
    `trakt_slug` for every Trakt payload, so nothing is lost by reading only it.

    THE SLUG WHEN TRAKT GAVE ONE, THE NUMERIC ID WHEN IT DID NOT. Both reach the
    same page — `trakt.tv/shows/<id>` redirects to the slugged form — so the
    choice is not about whether the link works but about who does the lookup.
    Sending the readable name spares Trakt one, which is what it and Simkl both
    ask callers to do.

    AND EITHER BEATS THE HOMEPAGE. A slug-less entry used to fall back to
    `https://trakt.tv`, which is the worst of the three outcomes: it is a link
    that appears to have worked, so nobody reports it, and it drops the viewer on
    a front page with no path back to the title they clicked. A page for the
    wrong-looking URL is still the right page; the homepage never is.
    """
    kind = "movies" if media == Media.MOVIE else "shows"
    name = ids.get("trakt_slug") or ids.get("trakt")
    return f"https://trakt.tv/{kind}/{name}" if name else "https://trakt.tv"


def episode_url(ids: dict, season: int, number: int) -> str:
    """https://app.trakt.tv/shows/<slug|id>?season=N&view=episode&episode=M

    THE SLUG IS PREFERRED AND THE ID IS THE FALLBACK, for the reason `title_url`
    gives: Trakt resolves its own numeric id perfectly well, but doing so costs
    it a title lookup and a redirect it would not have needed.

    A MAP WITH NEITHER GETS NO LINK. Trakt's slug arrives on history events, so a
    title with no recent play can carry an id for years and still have no name —
    the id alone reaches the right page, which is why it is a fallback rather
    than a second requirement.
    """
    name = str(ids.get("trakt_slug") or ids.get("trakt") or "")
    if not name:
        return ""
    return (f"https://app.trakt.tv/shows/{quote(name, safe='')}"
            f"?season={season}&view=episode&episode={number}")


def to_record(entry: dict, kind: Media) -> Record | None:
    """Turn a raw Trakt calendar entry into the uniform Record shape.

    VIEWER-INDEPENDENT BY CONSTRUCTION — no timezone is passed in and none is
    needed. The instant goes into `air_ts` and the four local spellings of it are
    `base.render`'s to derive, which is what lets one stored record serve every
    viewer of the window it sits in.

    IT TAKES A MEDIA AND NOT AN ENDPOINT, because a media is all it ever read off
    one — and an endpoint is exactly what a caller outside the fill does not
    have. The calendar SEARCH builds records from a per-title lookup, and the
    `show` object that lookup returns is the same shape as the one an entry
    carries; feeding this function a one-entry synthetic is what keeps a searched
    row and a filled row the same row, built by the same code, instead of two
    builders drifting apart field by field.
    """
    media = entry.get(str(kind)) or {}
    aired_raw = entry.get("first_aired") or entry.get("released")
    if not aired_raw or not media:
        return None

    # `released` (movies) is a plain DATE and `first_aired` an ISO UTC timestamp,
    # and the difference is not cosmetic — see Record.date_only. A date is parsed
    # at UTC midnight so it round-trips back to itself, and is flagged so nothing
    # downstream converts it into somebody's yesterday.
    date_only = "T" not in str(aired_raw)
    try:
        if date_only:
            dt = datetime.fromisoformat(f"{aired_raw}T00:00:00+00:00")
        else:
            dt = datetime.fromisoformat(str(aired_raw).replace("Z", "+00:00"))
    except ValueError:
        return None

    ids = media.get("ids") or {}
    episode = entry.get("episode") or {}
    ep_label = None
    ep_season = episode.get("season") if episode else None
    ep_number = episode.get("number") if episode else None
    if ep_season is not None and ep_number is not None:
        ep_label = f"S{int(ep_season):02d}E{int(ep_number):02d}"

    # Full overview is sent; cards clamp it via CSS, the poster-only panel scrolls it.
    overview = (media.get("overview") or "").strip()

    return Record(
        source=Source.TRAKT,
        media=kind,
        id=listing_id(ids),
        ids=_ids.normalize(ids),
        # THE SLUG IF TRAKT NAMED ONE, THE NUMERIC ID IF IT DID NOT. Trakt resolves
        # either — `/shows/<id>` reaches the same page `/shows/<slug>` does — but
        # the readable name saves it a title lookup, so it is what we send when we
        # have it. What this replaced sent every slug-less entry to the Trakt
        # HOMEPAGE, which is a link that looks like it worked and goes nowhere near
        # the title the viewer clicked.
        # Normalized first, because `title_url` reads the namespaced spelling
        # and a raw Trakt block carries the bare one.
        detail_url=title_url(kind, _ids.normalize(ids)),
        title=media.get("title") or "Untitled",
        year=media.get("year") or "",
        network=media.get("network") or "",
        country=(media.get("country") or "").upper(),
        language=(media.get("language") or "").upper(),
        runtime=media.get("runtime"),
        status=media.get("status") or "",
        rating=round(float(media["rating"]), 1) if media.get("rating") else None,
        # THE SLUGS, NOT THE DISPLAY FORM. Record.genres says why, and the symptom
        # of getting it wrong is a "game-show" filter that silently stops matching
        # while "drama" carries on working.
        genres=[str(g) for g in (media.get("genres") or [])],
        certification=(media.get("certification") or "").upper(),
        overview=overview,
        poster=poster(media),
        air_ts=epoch_seconds(dt),
        date_only=date_only,
        episode_label=ep_label,
        episode_title=episode.get("title") or "",
        season=int(ep_season) if ep_season is not None else None,
        episode_number=int(ep_number) if ep_number is not None else None,
    )
