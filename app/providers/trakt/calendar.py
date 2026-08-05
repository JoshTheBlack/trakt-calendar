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
from datetime import date, datetime
from urllib.parse import urlencode

from ...config import Settings
from ...endpoints import Endpoint
from ..base import Media, Record, Source, collect_ids
from . import transport
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

    Raises TraktError for a refusal, a non-200, or a body that will not parse. A
    body that parses to something other than a list reads as an empty window —
    there is nothing to salvage from it and nothing to report about it.
    """
    url = (
        f"{transport.API_BASE}/calendars/all/{calendar_path(endpoint)}/{start.isoformat()}/{days}"
        f"?{urlencode({'extended': 'full,images'})}"
    )
    t0 = _time.perf_counter()
    resp = await transport.send(transport.shared_client(), "GET", url,
                                headers=transport.api_headers(settings, paginate=False))
    _perf.debug("netGET    calendar/%s/%s+%dd -> %s  %.0fms", endpoint.key, start.isoformat(),
                days, resp.status_code, (_time.perf_counter() - t0) * 1000.0)
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
        record = to_record(entry, endpoint)
        if record is not None:
            records.append(record)
    return records


def poster(media: dict) -> str | None:
    imgs = media.get("images") or {}
    posters = imgs.get("poster") or []
    if posters:
        url = posters[0]
        if not url.startswith("http"):
            url = "https://" + url
        return url
    return None


def to_record(entry: dict, endpoint: Endpoint) -> Record | None:
    """Turn a raw Trakt calendar entry into the uniform Record shape.

    VIEWER-INDEPENDENT BY CONSTRUCTION — no timezone is passed in and none is
    needed. The instant goes into `air_ts` and the four local spellings of it are
    `base.render`'s to derive, which is what lets one stored record serve every
    viewer of the window it sits in.
    """
    media = entry.get(endpoint.media) or {}
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
        media=endpoint.media,
        id=ids.get("slug") or str(ids.get("trakt") or ""),
        ids=collect_ids(ids),
        detail_url=(
            f"https://trakt.tv/{'movies' if endpoint.media == Media.MOVIE else 'shows'}/{ids.get('slug')}"
            if ids.get("slug") else "https://trakt.tv"
        ),
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
        air_ts=dt.timestamp(),
        date_only=date_only,
        episode_label=ep_label,
        episode_title=episode.get("title") or "",
        season=int(ep_season) if ep_season is not None else None,
        episode_number=int(ep_number) if ep_number is not None else None,
    )
