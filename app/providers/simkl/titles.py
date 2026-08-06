"""Simkl's per-title CATALOG detail — GET /tv/{id} and GET /movies/{id} — the
calendar enrichment drain's only source of genres, country, certification,
network, runtime, status and overview: the fields Simkl's calendar CDN files
never carry at all (see calendar.py's Record docstring beside this module).

BOTH ENDPOINTS ANSWER FOR ANIME TOO, SO THERE IS NO THIRD ENDPOINT HERE.
Measured live 2026-08-06 against a known anime id (One Piece, simkl id 38636):
GET /tv/{id} returned every field GET /anime/{id} does — genres, country,
certification, network, `mapped_tvdb_seasons`, even `"type": "anime"` — so a
Simkl "show" Record, which by construction never records whether the title
behind it is a TV show or an anime (calendar.py's to_show_record folds both
into Media.SHOW), never needs a second lookup or a guess about which one to
try. This is also why migration 24's `simkl_titles.media` column keeps only
the app's own two-value vocabulary ('show'/'movie') rather than growing a
third 'anime' value: which detail endpoint answered is not a fact this table
ever needs to remember.

SIMKL'S "NOT FOUND" IS NOT AN HTTP STATUS, MEASURED LIVE THE SAME DAY. An id
with nothing behind it answers 200 with an empty JSON array (`[]`), not a 404.
Id 0, and a non-numeric id, both answer 200 with a page that is not a title at
all (a "top aired" digest). `/movies/{id}` does return a real 404 for at least
one case (id 0) — Simkl is not even consistent with ITSELF about this across
its two catalogues. Because none of that can be trusted as a status code, an
answer is accepted only when it actually PARSES as a title: a dict carrying
both `ids` and a `title`. Anything else — whatever the status code said — is
treated the same as a lookup that found nothing, and the caller backs off
exactly as it would for a genuine failure (see app/calendar/enrich.py).

THESE ENDPOINTS TAKE NO TOKEN and are cached through CATALOG_POOL exactly like
the tracker's per-title lookups in detail.py beside this module — same pool,
same "spends nobody's personal quota" reasoning, different fields kept. THIS
IS A SEPARATE MODULE FROM detail.py DELIBERATELY: that one already exists and
is the tracker's — it answers "what does a season look like" for the tiles the
tracker draws, and touching it for an unrelated calendar concern would mean
two features changing the same file for two different reasons. This module
answers a calendar question and nothing here is read by the tracker.
"""
from __future__ import annotations

from typing import Any

from ...config import Settings
from ..base import Media
from . import transport

# Catalog metadata barely moves — an episode gains a date, a status flips once
# a season away — so this sits on the same day-long TTL detail.py's episode
# lists use, for the same reason: short enough that a real change is picked up
# without anybody asking, long enough that a busy month's worth of titles does
# not re-fetch itself on every read.
DETAIL_CACHE_TTL_SECONDS = 24 * 60 * 60

_DETAIL_PATHS = {Media.SHOW: "tv", Media.MOVIE: "movies"}

# The id namespaces a detail payload can add over what the calendar file
# already gave a record (see providers/base.py's ID_KEYS / Record.ids).
# `simkl`, `slug`, `tmdb` and `imdb` all ride the calendar file already, and a
# detail payload's own copy of those is not worth a second source of truth for
# them — these three are the ones measured live to appear only on the detail
# endpoints, never on the calendar files.
_ID_UPGRADES = ("tvdb", "mal", "anidb")


def _genre_slug(name: str) -> str:
    """Simkl spells a genre "Game Show" — Title Case, spaces. Trakt's own
    calendar payload already arrives lower-cased and hyphenated ("game-show"),
    which is the spelling app/calendar/filter.py matches a viewer's spec
    against (see Record.genres). Without this, a filter spec written against
    Trakt's shape would silently never match the same genre on a Simkl title —
    exactly the kind of failure that is hardest to notice, per filter.py's own
    warning about title-casing. Not slug-safe for punctuation beyond a space;
    none was observed in a live sample, and an unrecognized one would simply
    keep missing that one genre rather than doing anything worse.
    """
    return str(name).strip().lower().replace(" ", "-")


def _looks_like_a_title(payload: Any) -> bool:
    """Whether `payload` is a real answer rather than one of the shapes Simkl
    hands back for an id it cannot place — see the module docstring for what
    those look like in practice."""
    return (isinstance(payload, dict)
            and isinstance(payload.get("ids"), dict)
            and bool(payload.get("title")))


def _extract(payload: dict) -> dict[str, Any]:
    """The subset of one detail payload this app keeps, in Record's own field
    names — genres already slugged, everything else taken as Simkl sends it."""
    ids = payload.get("ids") or {}
    upgraded_ids = {
        key: str(ids[key]) for key in _ID_UPGRADES if ids.get(key) not in (None, "")
    }
    runtime = payload.get("runtime")
    return {
        "genres": [_genre_slug(g) for g in (payload.get("genres") or []) if g],
        "network": str(payload.get("network") or ""),
        "country": str(payload.get("country") or ""),
        "certification": str(payload.get("certification") or ""),
        "runtime": int(runtime) if isinstance(runtime, (int, float)) else None,
        "status": str(payload.get("status") or ""),
        "overview": str(payload.get("overview") or ""),
        "ids": upgraded_ids,
    }


async def fetch_title(settings: Settings, simkl_id: int, media: Media | str) -> dict | None:
    """One title's enrichment fields, or None when Simkl could not answer for
    `simkl_id` — a real failure (network, rate limit, an instance-wide block)
    or an id it does not recognise. The caller (app/calendar/enrich.py) treats
    both the same way: record the attempt and back off, rather than guessing
    which one happened from a status code that measurement showed is not
    trustworthy here (see the module docstring).

    Cached exactly like every other Cloudflare-edged Simkl read — through
    CATALOG_POOL, on its own TTL — so a title several calendar windows
    reference costs one call regardless of how many times the drain is asked
    about it before that TTL turns over.
    """
    path = _DETAIL_PATHS.get(Media(media))
    if path is None or not simkl_id:
        return None
    try:
        payload = await transport.cached_get(
            transport.catalog_client(), settings, f"{path}/{simkl_id}", {},
            pool=transport.CATALOG_POOL, ttl_seconds=DETAIL_CACHE_TTL_SECONDS,
        )
    except transport.SimklError:
        return None
    if not _looks_like_a_title(payload):
        return None
    return _extract(payload)
