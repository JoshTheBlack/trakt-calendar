"""Reading one person's own TMDB account: the lists its v4 API keeps for them.

EVERY READ HERE IS PRIVATE. It carries the VIEWER's user token (the one TMDB's
sign-in issued them, swapped onto Settings by auth.viewer.settings_for_user) and
goes through transport.send_private, which never touches the shared response
cache: two people reading their own watchlist ask for the same address.

WHAT WAS MEASURED against the live service, 2026-11-08, on the owner's account
(every probe write undone afterwards):
  - GET /4/account/{account object id}/{tv|movie}/{watchlist|rated}?page=N
    answers 20 items a page with `total_pages` (0 for an empty list); a page
    past the end answers no items. The order is roughly the order things were
    added; `sort_by` made no visible difference, so nothing here relies on order.
  - An item is TMDB's catalogue entry — `id` (TMDB's own id, which is also the
    tmdb id the app files titles by), `name` / `title`, the first air or release
    date, `media_type` — and NO other service's ids. A rated item adds
    `account_rating: {value, created_at}`, the value on TMDB's 0.5-10 scale.
  - The WATCHLIST read is current the moment a write answers. The RATED lists
    LAG — seconds to minutes, in both directions — which matters to nobody here
    (a ratings seed reads what is there) but means a rating made a moment ago
    may not be seeded yet.
  - A grant the person revoked, or one revoked by an unlink, answers 401 with
    TMDB `status_code` 3.
"""
from __future__ import annotations

import logging
from urllib.parse import quote

from ...config import Settings
from ..base import Media
from . import transport

logger = logging.getLogger(__name__)

# How many pages one read may take. At 20 a page this is 10,000 titles — far
# past any list a person keeps by hand — and a read claiming more is not one
# this module understands: it stops rather than pages without end.
MAX_PAGES = 500

# TMDB's own word for each medium in an account path.
_KIND = {Media.SHOW: "tv", Media.MOVIE: "movie"}


def kind(media: Media) -> str:
    """TMDB's word for `media` in an account path ("tv", "movie")."""
    return _KIND[Media(media)]


def refused(status: int, what: str) -> transport.TmdbError:
    """The error for an account request TMDB refused. A 401 is the link itself
    — revoked on themoviedb.org, or never valid — and says so, because no retry
    mends it; anything else is TMDB not answering this time."""
    if status == 401:
        return transport.TmdbError(
            "TMDB refused this account's link (401). Connect TMDB again on your "
            "account page.", 401)
    return transport.TmdbError(f"TMDB refused {what} (HTTP {status}).", status)


async def every_page(settings: Settings, media: Media, listing: str) -> list[dict]:
    """Every item of one of the viewer's own lists (`listing`: "watchlist" or
    "rated"), every page. Raises TmdbError when any page cannot be read: a read
    missing a page would make the titles on it look absent, and a drop or an
    undo would then leave them where they are."""
    account = settings.tmdb_account_id.strip()
    if not account:
        raise transport.TmdbError("No TMDB account is linked.")
    path = f"account/{{account}}/{kind(media)}/{listing}"
    items: list[dict] = []
    page = 1
    while True:
        url = (f"{transport.API_V4}/account/{quote(account, safe='')}/{kind(media)}/"
               f"{listing}?page={page}")
        resp = await transport.send_private("GET", url, path, token=settings.tmdb_user_token)
        if resp.status_code != 200:
            logger.warning("TMDB %s read, page %d -> HTTP %s", path, page, resp.status_code)
            raise refused(resp.status_code, f"a read of the {listing}")
        try:
            body = resp.json()
        except ValueError:
            body = None
        if not isinstance(body, dict) or not isinstance(body.get("results"), list):
            raise transport.TmdbError(f"TMDB answered a {listing} read with no list.")
        items.extend(item for item in body["results"] if isinstance(item, dict))
        try:
            pages = int(body.get("total_pages") or 0)
        except (TypeError, ValueError):
            pages = 0
        if pages > MAX_PAGES:
            raise transport.TmdbError(
                f"TMDB says the {listing} runs to {pages} pages; {MAX_PAGES} is the most "
                "this app reads.")
        if page >= pages or not body["results"]:
            return items
        page += 1


async def ratings(settings: Settings) -> list[tuple[Media, dict]]:
    """Every title the viewer rated on TMDB, shows then films, as (media, item)
    pairs — each item carrying `account_rating`. Raises TmdbError when either
    list cannot be read whole."""
    out: list[tuple[Media, dict]] = []
    for media in (Media.SHOW, Media.MOVIE):
        out.extend((media, item) for item in await every_page(settings, media, "rated"))
    return out
