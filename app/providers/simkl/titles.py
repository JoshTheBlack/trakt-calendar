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
the answer to arrive. It does, in transport.send, and only after classifying
the target: see transport.py's redirect_pool for why the hop picks its own
pool rather than riding the one the /tv/{id} call was issued on. This is also why migration 24's `simkl_titles.media`
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
IS A SEPARATE MODULE FROM detail.py DELIBERATELY: that one answers "what does
one title look like right now" for whoever is drawing it, and this one answers
"what does the calendar keep about a title, forever, in a row on disk". The
two have different reasons to change — this one's output shape is versioned and
a change to it re-fetches every stored row (EXTRACT_VERSION below), while a
change to what a modal draws costs nothing.

WHO READS THIS: app/calendar/enrich.py's drain, which stores what comes back,
and detail.py's `fetch_details`, which draws the modal from the same record
rather than issuing a second lookup of its own. The tracker's tiles do not —
they ask detail.py for a season, which is a different endpoint.
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
#
# 3 IS THE MOVIE FIELDS: language, year, released, director, budget, the
# audience rating, and the reduced release-country map below. Every row this
# app has written so far predates all of them, so bumping this is what makes
# the existing table refresh itself into the wider shape rather than sitting on
# the narrower one until its 30-day retention window expires.
EXTRACT_VERSION = 3

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


# TMDB's release-type numbering, which Simkl reproduces verbatim in each
# `release_dates[].results[].type`. Named here rather than left as bare
# integers because the numbers are meaningless on sight and are the whole
# vocabulary a viewer's release filter is written in — see
# app/calendar/filter.py, which imports these rather than respelling them.
RELEASE_PREMIERE = 1
RELEASE_LIMITED = 2
RELEASE_THEATRICAL = 3
RELEASE_DIGITAL = 4
RELEASE_PHYSICAL = 5
RELEASE_TV = 6

# In the order a viewer would read them: how a film reaches an audience, from
# the first screening to the last format. The labels are the ones the filter UI
# draws, so they live beside the numbers they name.
RELEASE_TYPE_LABELS = {
    RELEASE_PREMIERE: "Premiere",
    RELEASE_LIMITED: "Limited theatrical",
    RELEASE_THEATRICAL: "Theatrical",
    RELEASE_DIGITAL: "Digital",
    RELEASE_PHYSICAL: "Physical",
    RELEASE_TV: "TV",
}


def _release_types_by_country(release_dates: Any) -> dict[str, list[int]]:
    """Simkl's per-country `release_dates` list, reduced to {country: [type]}.

    THE DATES ARE DELIBERATELY DROPPED, AND THAT IS THE "REDUCED FORM" THIS IS.
    What the app does with this is decide WHICH TITLES A VIEWER SEES — a films
    calendar that lists every release in every market is unusable without a way
    to say "the ones released here, in the formats I care about" (measured on a
    live instance: 1314 August titles, 444 of them with a US release block at
    all). Deciding which DATE a card is drawn on is a different feature
    entirely, and one the calendar cannot do from here anyway: a film's digital
    release in September is not in August's stored window, so no amount of
    per-title detail read during an August read would put it there. Storing the
    dates would therefore be storing a field nothing can read, on every enriched
    movie row, forever — and adding them later costs nothing but an
    EXTRACT_VERSION bump, because `simkl_titles.payload` is an opaque blob.

    THE SHAPE, MEASURED OVER A LIVE MONTH (1314 movies, 2026-08-07): 1150 carry
    exactly one country block and the rest carry 2 to 13, so the map is small.
    The commonest blocks are US (444), GB (132), CH (107), AU (84) and BR (76);
    the commonest types are theatrical (755), premiere (752) and digital (568),
    with physical (7) and TV (41) genuinely rare.

    Country codes are upper-cased and types are de-duplicated and sorted, so one
    title's map is one value however Simkl happened to order the payload — the
    same reason `_genre_slug` exists one function up.
    """
    out: dict[str, list[int]] = {}
    for block in release_dates or []:
        if not isinstance(block, dict):
            continue
        country = str(block.get("iso_3166_1") or "").strip().upper()
        if not country:
            continue
        types = out.setdefault(country, [])
        for result in block.get("results") or []:
            if not isinstance(result, dict):
                continue
            kind = result.get("type")
            # Simkl has only ever been observed sending the six TMDB numbers,
            # but an unknown one is kept rather than dropped: a viewer whose
            # filter names nothing it recognises still sees the title, which is
            # the safe direction, and dropping it would make a future seventh
            # type silently invisible.
            if isinstance(kind, int) and kind not in types:
                types.append(kind)
    return {country: sorted(types) for country, types in out.items() if types}


def _simkl_rating(ratings: Any) -> float | None:
    """Simkl's OWN audience score out of `ratings`, or None.

    ONLY SIMKL'S. The payload also carries imdb's (and, on some titles, a
    droprate), and `Record.rating` is one number shown under one service's
    mark — the card draws two services' ratings side by side and never averages
    them, precisely because two audiences produce two real numbers. Putting
    imdb's figure in the field labelled Simkl would be the same untruth in a
    quieter place. imdb's is left in the payload's shadow deliberately: nothing
    in this app has a surface for a third party's rating yet, and inventing one
    here would be a field with no reader.

    MEASURED, MOST MOVIES HAVE NONE — 84 of 1314 in a live August, because a
    global release calendar is mostly small and unreleased titles nobody has
    voted on. That is Simkl not having the data, not this app discarding it.
    """
    if not isinstance(ratings, dict):
        return None
    simkl = ratings.get("simkl")
    if not isinstance(simkl, dict):
        return None
    value = simkl.get("rating")
    return float(value) if isinstance(value, (int, float)) else None


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
    `airs` (a nested day/time/timezone object) was left out: cheap, but nothing
    reads it, and — like every other field here — adding one later costs no
    migration, since simkl_titles.payload is an opaque blob.

    THE MOVIE HALF, MEASURED OVER 1314 REAL FILMS FROM ONE LIVE AUGUST rather
    than assumed, because the coverage is what decides whether a field is worth
    a line: language 100%, released 100%, year 100%, director 92%, country 67%,
    certification 10%, budget 9%, ratings 6% — and `release_dates` on every
    single one. `language`, `year` and the audience `rating` land on Record
    fields the card already draws, which is why a movie card showed no language
    and no rating: the fields existed and nothing filled them in. `released`,
    `director` and `budget` have no Record field and no surface yet, and are
    kept for the same reason `trailers` and `total_episodes` are — they cost one
    short scalar each on a row that is being written anyway, and a detail modal
    that wants them should not have to re-fetch 1300 titles to get them.

    CERTIFICATION IS NOT MISSING AND MUST NOT BE "FIXED". It has been kept since
    this function was written and it is empty on ~90% of movies because Simkl
    does not have it (compare shows at 22% and anime at 90%). A blank
    certification chip on a film is the data, not a defect.

    DELIBERATELY STILL EXCLUDED: `alt_titles`, `relations`,
    `users_recommendations` and `fanart`. The middle two are the tens-of-
    kilobytes fields the whitelist exists for; the other two have no reader, and
    the author declined fanart by name.
    """
    ids = payload.get("ids") or {}
    full_ids = {
        str(key): str(value) for key, value in ids.items() if value not in (None, "")
    }
    runtime = payload.get("runtime")
    total_episodes = payload.get("total_episodes")
    year = payload.get("year")
    budget = payload.get("budget")
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
        # The three that land straight on a Record field the card already draws.
        # `language` arrives as Simkl's own two-letter code ("EN", "RU"), which
        # is the spelling Trakt uses too, so a merged card's two answers are
        # comparable without either side being respelled.
        "language": str(payload.get("language") or ""),
        "year": int(year) if isinstance(year, (int, float)) else "",
        "rating": _simkl_rating(payload.get("ratings")),
        # Which markets have a release and in what formats — the only thing a
        # viewer can narrow a global release calendar by. See
        # _release_types_by_country for why the dates themselves are dropped.
        "release_types_by_country": _release_types_by_country(payload.get("release_dates")),
        # No Record field and no surface yet; kept because they are one scalar
        # each on a row already being written. `released` is Simkl's own release
        # date as a plain YYYY-MM-DD string and is NOT what the calendar is
        # dated from — the calendar file's own per-month entry is (see
        # calendar.py's to_movie_record), and a title can carry several
        # releases while an entry carries one date.
        "released": str(payload.get("released") or ""),
        "director": str(payload.get("director") or ""),
        "budget": int(budget) if isinstance(budget, (int, float)) else None,
    }


async def fetch_title(settings: Settings, simkl_id: int, media: Media | str,
                      *, cache_only: bool = False) -> dict | None:
    """One title's catalogue fields, or None when Simkl could not answer for
    `simkl_id` — a real failure (network, rate limit, an instance-wide block)
    or an id it does not recognise. The enrichment drain (app/calendar/enrich.py)
    treats both the same way: record the attempt and back off, rather than
    guessing which one happened from a status code that measurement showed is not
    trustworthy here (see the module docstring).

    Cached exactly like every other Cloudflare-edged Simkl read — through
    CATALOG_POOL, on its own TTL — so a title several calendar windows
    reference costs one call regardless of how many times the drain is asked
    about it before that TTL turns over.

    `cache_only=True` makes no outbound call and answers None when nothing is
    cached, which is a THIRD indistinguishable way of failing and is handled by
    the same caller branch. It is what the public share pages read with: a
    visitor's click serves what the owner's own views already warmed and can
    never spend the instance's Simkl budget.
    """
    path = _DETAIL_PATHS.get(Media(media))
    if path is None or not simkl_id:
        return None
    try:
        payload = await transport.cached_get(
            transport.catalog_client(), settings, f"{path}/{simkl_id}", {},
            pool=transport.CATALOG_POOL, ttl_seconds=DETAIL_CACHE_TTL_SECONDS,
            cache_only=cache_only,
        )
    except transport.SimklError:
        return None
    if not _looks_like_a_title(payload):
        return None
    return _extract(payload)
