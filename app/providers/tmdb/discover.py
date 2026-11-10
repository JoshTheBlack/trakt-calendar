"""TMDB's discover listings, read whole: every page of one query over a span of
days, as the entries TMDB returned.

THE ONE READER OF A DISCOVER LISTING, because three things ask one: the Series
Premieres calendar (`first_air_date`), the Movie Premieres calendar
(`release_date` in the instance's region) and the season-premieres derivation's
"which shows aired this week" (`air_date`, premieres.py). What each MEANS is its
caller's; how a listing is paged, capped and cached is said once, here.

EVERY PAGE IS A PUBLIC CATALOGUE READ through transport.cached_get, so each is
cached under its own address and revalidated with its ETag past
`transport.DEFAULT_TTL_SECONDS` — a revalidated page TMDB has not changed costs
it no body. `include_adult=false` is on every query (a discover listing is what
a calendar draws, for every viewer).
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import AbstractAsyncContextManager, nullcontext
from datetime import date, timedelta

from ...config import Settings
from . import transport

logger = logging.getLogger(__name__)

# TMDB STOPS A DISCOVER LISTING AT PAGE 500, twenty entries a page: a query
# matching more than ten thousand titles answers only the first ten thousand.
# A span that would pass it is asked again as two halves (`entries`), which is
# what keeps every title in it — the measured busiest week of shows with an
# episode airing was 2,466 titles, 124 pages, so the cap is far off, and a
# listing that grew past it would otherwise lose its tail without a word.
PAGE_CAP = 500

# THE ORDER A LISTING IS PAGED IN, which TMDB needs to be the same from one page
# to the next or a title can slip between pages. Popularity is TMDB's own
# default and is recomputed in a daily batch, so the pages of one read agree;
# MEASURED on the week being written, one title in 1,750 appeared on two pages,
# which `entries` folds.
_SORT = "popularity.desc"


def query(date_field: str, first: date, last: date, **params) -> dict:
    """The parameters of one discover query over [first, last], both inclusive —
    TMDB's `.gte`/`.lte` bounds are days, and a day is inclusive at both ends."""
    return {**params, f"{date_field}.gte": first.isoformat(),
            f"{date_field}.lte": last.isoformat(), "include_adult": "false",
            "sort_by": _SORT}


async def _page(settings: Settings, kind: str, params: dict, page: int,
                gate: AbstractAsyncContextManager) -> dict:
    async with gate:
        try:
            body = await transport.cached_get(settings, f"discover/{kind}",
                                              {**params, "page": page})
        except transport.TmdbNotFound:
            # A page TMDB has no answer for is an empty page, not a failure: the
            # count of pages comes from TMDB's own first answer.
            return {}
    return body if isinstance(body, dict) else {}


async def entries(settings: Settings, kind: str, date_field: str, first: date, last: date,
                  *, gate: AbstractAsyncContextManager | None = None, **extra) -> list[dict]:
    """Every entry of `discover/{kind}` with `date_field` in [first, last], each
    title once, in no particular order; `extra` is the query's own narrowing (a
    region, release types). Raises transport.TmdbError when TMDB could not be
    asked for a page — never a short listing presented as whole.

    `gate` bounds how many of this read's pages are in flight at once, for a
    caller that must leave the client to others (the derivation runs behind the
    pages viewers are waiting on); without one, the transport's own gate is the
    only bound, which is what a fill wants.
    """
    gate = gate or nullcontext()
    params = query(date_field, first, last, **extra)
    head = await _page(settings, kind, params, 1, gate)
    pages = int(head.get("total_pages") or 0)
    if pages > PAGE_CAP and first < last:
        middle = first + timedelta(days=(last - first).days // 2)
        halves = await asyncio.gather(
            entries(settings, kind, date_field, first, middle, gate=gate, **extra),
            entries(settings, kind, date_field, middle + timedelta(days=1), last, gate=gate,
                    **extra))
        return _each_once([*halves[0], *halves[1]])
    if pages > PAGE_CAP:
        logger.warning("TMDB lists more than %d pages of discover/%s for %s alone; "
                       "reading the first %d", PAGE_CAP, kind, first, PAGE_CAP)
    rest = await asyncio.gather(*(_page(settings, kind, params, page, gate)
                                  for page in range(2, min(pages, PAGE_CAP) + 1)))
    return _each_once([entry for body in (head, *rest)
                       for entry in body.get("results") or () if isinstance(entry, dict)])


def _each_once(found: list[dict]) -> list[dict]:
    seen: set = set()
    out = []
    for entry in found:
        if entry.get("id") in seen:
            continue
        seen.add(entry.get("id"))
        out.append(entry)
    return out
