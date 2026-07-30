"""Trakt's calendar endpoints, and the normalizer that turns what they return
into the uniform `Item` every calendar source produces.

Fetching and normalizing are kept together here because they are two halves of
one answer to one question — "what airs this month" — but normalize() itself is
pure: it takes a raw entry and returns an Item, and is called with entries that
never came from this module at all (the calendar cache replays stored ones
through it).
"""
from __future__ import annotations

# The stdlib module, aliased only so a reader of a file that is itself called
# calendar.py can tell the two apart at the call site.
import calendar as _calendar
import logging
import time as _time
from datetime import date, datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from ... import calendar_filter
from ...config import Settings
from ...endpoints import Endpoint
from ..base import Item, Media, Source, collect_ids
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


async def fetch_window(endpoint: Endpoint, settings: Settings, start: date, days: int) -> list[dict]:
    """One window of Trakt's calendar, exactly as Trakt returned it.

    THE ONLY PLACE THIS APP ASKS TRAKT WHAT AIRS. Everything downstream — the
    month fetch below, and the cache's own pruned-window fetch — is this plus
    what that caller does with the answer. It was two copies until they began to
    drift, which is what a second copy looks like: the same thirty lines, and two
    different wordings of the same pagination warning.

    Returns the raw entries, unfiltered and unnormalized, because the two callers
    filter differently: an uncached month applies the viewer-facing genre/country
    reproduction, while the cache applies the instance-wide content floor
    (certifications included) before anything is stored. Deciding that here would
    make one of them wrong.

    genres/countries are NOT sent as query params. The calendar cache stores the
    complete unfiltered result so those can be read-time per-viewer filters —
    one viewer including JP/KR shows and another excluding them from the same
    cached data — and the equivalent filtering is reproduced client-side via
    app/calendar_filter.py.

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
    return raw if isinstance(raw, list) else []


async def fetch_calendar(endpoint: Endpoint, settings: Settings, year: int, month: int) -> list[Item]:
    """Fetch and normalize a month of calendar items for the given endpoint."""
    days = _calendar.monthrange(year, month)[1]
    start_date = f"{year:04d}-{month:02d}-01"
    end_date = f"{year:04d}-{month:02d}-{days:02d}"

    raw = await fetch_window(endpoint, settings, date(year, month, 1), days)

    # Trakt used to filter by the genres/countries query params server-side;
    # those are no longer sent, so the same filtering is reproduced here on the
    # raw genre slugs (before normalization, which would rewrite "game-show" to
    # "Game Show"), giving an item set identical to what Trakt returned before.
    raw = calendar_filter.filter_entries(raw, endpoint.media, settings.genres, settings.countries)

    tz = ZoneInfo(settings.timezone)
    items = [normalize(entry, endpoint, tz) for entry in raw]
    items = [i for i in items if i and start_date <= i.air_date <= end_date]

    # Network filter: an operator-configured allow-list, matched case-sensitively
    # against Trakt's own network naming.
    if settings.network_filter:
        allow = set(settings.network_filter)
        items = [i for i in items if i.network in allow]

    items.sort(key=lambda i: i.air_ts)
    return items


def poster(media: dict) -> str | None:
    imgs = media.get("images") or {}
    posters = imgs.get("poster") or []
    if posters:
        url = posters[0]
        if not url.startswith("http"):
            url = "https://" + url
        return url
    return None


def normalize(entry: dict, endpoint: Endpoint, tz: ZoneInfo) -> Item | None:
    """Turn a raw Trakt calendar entry into the uniform Item shape."""
    media = entry.get(endpoint.media) or {}
    aired_raw = entry.get("first_aired") or entry.get("released")
    if not aired_raw or not media:
        return None

    # `released` (movies) is a plain date; `first_aired` is an ISO UTC timestamp.
    try:
        if "T" in str(aired_raw):
            dt = datetime.fromisoformat(str(aired_raw).replace("Z", "+00:00")).astimezone(tz)
        else:
            dt = datetime.fromisoformat(f"{aired_raw}T00:00:00+00:00").astimezone(tz)
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

    return Item(
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
        genres=[g.replace("-", " ").title() for g in (media.get("genres") or [])],
        certification=(media.get("certification") or "").upper(),
        overview=overview,
        poster=poster(media),
        air_date=dt.strftime("%Y-%m-%d"),
        air_ts=dt.timestamp(),
        air_display=dt.strftime("%d %b %Y"),
        air_time=dt.strftime("%H:%M"),
        day_of_week=dt.strftime("%A"),
        episode_label=ep_label,
        episode_title=episode.get("title") or "",
        season=int(ep_season) if ep_season is not None else None,
        episode_number=int(ep_number) if ep_number is not None else None,
    )
