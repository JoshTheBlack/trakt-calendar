"""Bringing a frozen month's stored films into the shape the rest of the app
reads — once, and only where they are not already.

WHY THERE ARE TWO SHAPES. A closed month keeps its films as a snapshot
(`distrakt_months.movies_json`). Snapshots written before films had a shared
identity hold `{trakt_id, title, year, watched_at}`; everything written since
holds `{key, ids, title, year, watched_at}`, keyed like every other title
(`movie:tmdb:…`). Migration 18 moved every other table onto keys and left these
alone on purpose — nothing in the database could say what a Trakt-only film's
shared id was, and a migration cannot ask.

WHAT THAT COST, MEASURED on the owner's database: 18 of 37 stored films in the old
shape. A film added to such a month later — the history backfill adds film by
film, keyed — was not recognised as already there, so 13 films showed twice; and
the remove control names a film by its key, so no old-shape film could be
removed at all.

SO THIS KEYS THEM, cheapest answer first:
  - an old entry whose month already holds the same title and year in the keyed
    shape IS that film, and is dropped — no call;
  - otherwise its Trakt id is looked up (the same cached, public summary every
    film panel reads), and it is rewritten keyed, or dropped if that key is
    already on the month;
  - a film Trakt cannot answer for is left exactly as it was, and not asked
    about again for a day.
"""
from __future__ import annotations

import logging

from .. import cache
from ..providers.base import Media, resolve_key
from ..providers.trakt import TraktError, detail as trakt_detail
from . import store

logger = logging.getLogger(__name__)

# How long a film Trakt could not answer for waits before it is asked again.
REASK_SECONDS = 24 * 60 * 60


async def repair(user_id: int, settings) -> int:
    """Key every old-shape film on this viewer's frozen months. Returns how many
    months changed — 0 on every pass once nothing old is left, which is one
    indexed read."""
    changed = 0
    for month, films in await store.months_with_unkeyed_films(user_id):
        fixed = await keyed(settings, films)
        if fixed != films:
            await store.set_month_movies(user_id, month, fixed)
            logger.info("tracker films: %s for user %s — %d stored film(s) became %d",
                        month, user_id, len(films), len(fixed))
            changed += 1
    return changed


async def keyed(settings, films: list[dict]) -> list[dict]:
    """`films` with every old-shape entry keyed or dropped, in their own order;
    what cannot be resolved stays as it was."""
    keys = {str(film["key"]) for film in films if film.get("key")}
    named = {(str(film.get("title") or "").casefold(), str(film.get("year") or ""))
             for film in films if film.get("key")}
    out = []
    for film in films:
        if film.get("key") or not film.get("trakt_id"):
            out.append(film)
            continue
        if (str(film.get("title") or "").casefold(), str(film.get("year") or "")) in named:
            continue  # the keyed copy beside it is this film
        ids = await _trakt_ids(settings, film["trakt_id"])
        key = resolve_key(Media.MOVIE, ids) if ids else None
        if key is None:
            out.append(film)
            continue
        if str(key) in keys:
            continue
        keys.add(str(key))
        out.append({"key": str(key), "ids": ids, "title": film.get("title") or "",
                    "year": film.get("year"), "watched_at": film.get("watched_at")})
    return out


async def _trakt_ids(settings, trakt_id) -> dict:
    """Trakt's ids for a film, or {} when it cannot say — remembered for a day,
    so an unanswerable film is not asked about on every load."""
    marker = f"film_snapshot_unanswered:{trakt_id}"
    if not getattr(settings, "trakt_catalogue_configured", False):
        return {}
    if await cache.get(marker, REASK_SECONDS):
        return {}
    try:
        summary = await trakt_detail.fetch_movie_summary(settings, trakt_id)
    except TraktError as exc:
        logger.info("tracker films: Trakt could not be asked about film %s (%s)", trakt_id, exc)
        return {}
    ids = trakt_detail.ids_map(summary or {})
    if resolve_key(Media.MOVIE, ids) is None:
        await cache.set(marker, True)
        return {}
    return ids
