"""Simkl's per-title CATALOG detail — GET /tv/{id} and GET /movies/{id} — the
calendar enrichment drain's only source of genres, country, certification,
network, runtime, status and overview: the fields Simkl's calendar CDN files
never carry at all (see calendar.py's Record docstring beside this module).

BOTH ENDPOINTS ANSWER FOR ANIME TOO, SO THERE IS NO THIRD ENDPOINT HERE —
BUT "ANSWER" SOMETIMES MEANS "REDIRECT", NOT A 200 BODY. Measured live
2026-08-06 against a known anime id (One Piece, simkl id 38636), GET /tv/{id}
returned every field GET /anime/{id} does directly, in one call. A second,
larger measurement the same day — 94 ids this app's own drain had already
recorded as an empty answer, pulled from a live instance — found GET
/tv/{id} instead answering 302 to GET /anime/{id} for every one of them:
Simkl serves some anime straight off /tv/{id} and redirects the rest, and
there is no way to tell which in advance. A Simkl "show" Record still never
needs a second LOOKUP or a guess about which endpoint to try — it always
asks /tv/{id} — but the transport has to actually FOLLOW that redirect for
the answer to arrive; see transport.py's CATALOG_POOL for where that is
turned on and why. This is also why migration 24's `simkl_titles.media`
column keeps only the app's own two-value vocabulary ('show'/'movie') rather
than growing a third 'anime' value: which detail endpoint answered is not a
fact this table ever needs to remember.

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

# Bumped whenever _extract's OUTPUT SHAPE changes in a way a stored
# simkl_titles row must not be mistaken for already having. A row this app
# wrote before this field existed at all — the entire narrower-extraction
# generation, ~260 of them measured live on 2026-08-06 — has no key here,
# which reads as "a version this constant does not recognise" exactly as
# safely as an explicit mismatch would. app/calendar/enrich.py's drain reads
# this to decide whether an already-successful row is actually DONE or only
# owed a re-fetch under the wider extraction — see its own comment for why
# that is the only place the distinction has to be made: overlay_records
# still applies whatever an old row happens to carry in the meantime, so a
# title already enriched under the old shape does not regress to unenriched
# while it waits its turn to be re-fetched.
EXTRACT_VERSION = 2

# MEASURED ACROSS 300 REAL TITLES, THREE DISJOINT SAMPLES (2026-08-06): Simkl's
# `ids` map is not a fixed namespace set. Besides the three this app used to
# keep alone (tvdb, mal, anidb) it also returned tmdb, imdb, traktslug,
# tvdbslug, anilist, kitsu and mdlslug on a meaningful fraction of titles, and
# two namespaces — `tmdbtv` and `trakttvslug` — that appeared exactly once
# each across the whole sample and never again in a later round. An allowlist
# would have silently dropped those two, and would drop whatever Simkl adds
# next; the whole map is kept instead (see _extract). tmdb (40/45, then 84/105,
# then 137/150) and imdb (28/45, then 72/105, then 98/150) matter most: they
# are exactly the ids app/providers/base.py's identity waterfall already
# matches Trakt and Simkl records on, so a Simkl-only title enrichment gives a
# tmdb id to can land on the SAME calendar group as its Trakt counterpart with
# no matcher of its own — the mechanism already exists, this only feeds it.


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
    names — genres already slugged, everything else taken as Simkl sends it.

    THE `ids` MAP IS KEPT WHOLE, THE REST STAYS A WHITELIST. Ids are small,
    bounded (a dozen or so short strings) and the one place completeness beats
    selectivity — see EXTRACT_VERSION's neighbour comment above for why an
    allowlist there was actively wrong. `relations` and `users_recommendations`
    are the opposite case: measured to run tens of kilobytes each, and neither
    is read by anything this app does, so they are the reason the REST of the
    payload stays a named list rather than "store what Simkl sent".

    type/anime_type/trailers/total_episodes/poster/first_aired were all
    measured present and cheap (single scalars or one short list) and are
    kept for the reasons named where each is consumed: `type` drives the
    anime genre slug below and app/calendar/filter.py's film prune keys on
    `anime_type` (both need the ENRICHED value, not a guess from the redirect
    a title happened to take to get here). `trailers` is what the detail
    modal work already on this app's roadmap is documented to want.
    `total_episodes`, `poster` and `first_aired`
    cost nothing to keep and round out what a future card/modal can draw on
    without a second lookup — but note total_episodes is NOT a film signal
    (see the filter module's own docstring for the measured counter-example).
    `airs` (a nested day/time/timezone object) and `ratings` (nested, multi-
    source) were left out: cheap individually, but nothing shipping in this
    change reads either, and — like every other field here — adding one later
    costs no migration, since simkl_titles.payload is an opaque blob.
    """
    ids = payload.get("ids") or {}
    full_ids = {
        str(key): str(value) for key, value in ids.items() if value not in (None, "")
    }
    runtime = payload.get("runtime")
    total_episodes = payload.get("total_episodes")
    genres = [_genre_slug(g) for g in (payload.get("genres") or []) if g]
    # THE PAYLOAD SAYS WHETHER A TITLE IS ANIME OUTRIGHT ("type": "anime"), SO
    # THIS DOES NOT INFER IT FROM WHICH ENDPOINT ANSWERED OR WHETHER A REDIRECT
    # WAS FOLLOWED. Giving it the same "anime" genre slug Trakt titles can
    # already carry makes Simkl's anime filterable by the control that already
    # filters Trakt's, with no new filter dimension.
    if str(payload.get("type") or "").lower() == "anime" and "anime" not in genres:
        genres.append("anime")
    return {
        "extract_version": EXTRACT_VERSION,
        "genres": genres,
        "network": str(payload.get("network") or ""),
        "country": str(payload.get("country") or ""),
        "certification": str(payload.get("certification") or ""),
        "runtime": int(runtime) if isinstance(runtime, (int, float)) else None,
        "status": str(payload.get("status") or ""),
        "overview": str(payload.get("overview") or ""),
        "ids": full_ids,
        "type": str(payload.get("type") or ""),
        # THE ONE FIELD THE FILM PRUNE ACTUALLY KEYS ON. Measured live: `ona`,
        # `ova`, `tv` and `special` are all SERIAL formats (ONA = Original Net
        # Animation, an anime series released to the web, not a film) — only
        # `movie` means "this is a film". See app/calendar/filter.py's
        # prune_disguised_films for where that distinction is spent.
        "anime_type": str(payload.get("anime_type") or ""),
        "total_episodes": int(total_episodes) if isinstance(total_episodes, (int, float)) else None,
        "poster": str(payload.get("poster") or ""),
        "first_aired": str(payload.get("first_aired") or ""),
        "trailers": payload.get("trailers") if isinstance(payload.get("trailers"), list) else [],
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
