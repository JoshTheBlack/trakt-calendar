"""TMDB's payloads in this app's vocabulary. PURE — no I/O, no settings.

ONE READER PER PAYLOAD KIND, and every module in this package calls it. The
details panel, the season totals, the season picker and the calendar's record
builder all read the same `/tv/{id}` object, and two readings of one payload is
how fields drift apart: the Simkl package's enrichment extraction and its detail
projection once disagreed about a title's genres for exactly that reason. So a
title's genres, certification, runtime, status, ids and poster are each decided
once, here, and a new caller asks rather than re-reads.

TMDB's DOCUMENTED SHAPES ARE WHAT THIS READS, checked against real answers saved
under tests/fixtures/tmdb/ (trimmed, with no credential in them).
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Any

from ... import youtube
from .. import season as season_rules
from ..base import (Media, Record, Source, Video, VideoKind, collect_ids,
                    epoch_seconds)
from . import transport

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Genres
# ---------------------------------------------------------------------------

# TMDB's genre, by its ID, as this app's slugs (app/calendar/vocab.py's genre
# vocabulary, which every genre filter matches on). BY ID AND NOT BY NAME,
# because TMDB writes a genre's NAME in the instance's TMDB language — "Krimi" in
# German — while its id is the same everywhere; a table keyed on English names
# would stop matching the day an operator changed the language.
#
# EVERY GENRE TMDB LISTS IS NAMED HERE, both lists (GET /genre/tv/list and
# /genre/movie/list, saved as tests/fixtures/tmdb/genre_*_list.json), so each
# mapping is a decision rather than a fallback:
#   - TMDB's combined TV genres split into both of the app's: Action & Adventure
#     is action AND adventure, Sci-Fi & Fantasy is science-fiction AND fantasy;
#   - War & Politics is war — the app has no politics genre to put it in;
#   - Kids is family, Talk is talk-show, Music is musical — the app's names for
#     the same shelf;
#   - TV Movie maps to NOTHING. It is a format, not a genre, and the film
#     calendar already knows a film is a film; a "tv-movie" slug would be a chip
#     no vocabulary offers and a filter nobody could set.
# A GENRE TMDB ADDS LATER, WITH NO ENTRY HERE, is slugged from its name (`_slug`)
# and logged: still filterable by typing it, and the log line is the prompt to
# give it a real entry. That fallback is this table's own decision, written here.
GENRE_SLUGS: dict[int, tuple[str, ...]] = {
    # Shared by both lists.
    16: ("animation",), 35: ("comedy",), 80: ("crime",), 99: ("documentary",),
    18: ("drama",), 10751: ("family",), 9648: ("mystery",), 37: ("western",),
    # TV only.
    10759: ("action", "adventure"), 10762: ("family",), 10763: ("news",),
    10764: ("reality",), 10765: ("science-fiction", "fantasy"), 10766: ("soap",),
    10767: ("talk-show",), 10768: ("war",),
    # Film only.
    28: ("action",), 12: ("adventure",), 14: ("fantasy",), 36: ("history",),
    27: ("horror",), 10402: ("musical",), 10749: ("romance",),
    878: ("science-fiction",), 10770: (), 53: ("thriller",), 10752: ("war",),
}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def genre_slugs(genres) -> list[str]:
    """TMDB's `genres` ([{id, name}]) as this app's slugs, in TMDB's order, each once."""
    out: list[str] = []
    for genre in genres or ():
        if not isinstance(genre, dict):
            continue
        mapped = GENRE_SLUGS.get(genre.get("id"))
        if mapped is None:
            name = str(genre.get("name") or "")
            logger.info("TMDB genre %s (%r) has no mapping; slugging its name",
                        genre.get("id"), name)
            mapped = (_slug(name),) if name else ()
        out.extend(slug for slug in mapped if slug and slug not in out)
    return out


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

# TMDB's status words as the lower-case words Trakt's catalogue uses, which is
# the vocabulary a stored record and a filter already speak. Every value TMDB
# documents for a show and for a film is named, and each happens to be Trakt's
# word lower-cased — listed anyway, so the day TMDB adds one it is a visible gap
# rather than a silent pass-through. A status with no entry is lower-cased.
STATUS_WORDS = {
    # Shows.
    "Returning Series": "returning series", "Planned": "planned",
    "In Production": "in production", "Ended": "ended", "Canceled": "canceled",
    "Pilot": "pilot",
    # Films.
    "Rumored": "rumored", "Post Production": "post production",
    "Released": "released",
}


def status_word(status) -> str:
    text = str(status or "").strip()
    return STATUS_WORDS.get(text, text.lower())


# ---------------------------------------------------------------------------
# Certification, country, language, runtime, rating, ids, poster
# ---------------------------------------------------------------------------

# Whose certification a title shows: the United States' where TMDB lists one,
# because that is the rating vocabulary this app's certification filter offers
# (TV Parental Guidelines and the MPA's), and otherwise the rating of the
# country the title comes FROM — its own country's rating is the one most
# likely to exist, and any other country's is a guess about an audience.
_CERTIFICATION_COUNTRY = "US"


def certification(payload: dict, media: Media) -> str:
    """The title's rating, upper-cased: the US rating, else the rating of its
    first origin country, else "". A show reads `content_ratings`; a film reads
    `release_dates`, where each country lists a rating per release."""
    by_country = (_show_ratings(payload) if media is Media.SHOW
                  else _film_ratings(payload))
    for country in (_CERTIFICATION_COUNTRY, country_code(payload)):
        if rating := by_country.get(country):
            return rating
    return ""


def _show_ratings(payload: dict) -> dict[str, str]:
    out = {}
    for entry in (payload.get("content_ratings") or {}).get("results") or ():
        rating = str(entry.get("rating") or "").strip().upper()
        if rating:
            out.setdefault(str(entry.get("iso_3166_1") or "").upper(), rating)
    return out


def _film_ratings(payload: dict) -> dict[str, str]:
    """{country: the rating of its earliest-typed release that has one}. A film
    is rated per release — premiere, theatrical, digital — and the premiere often
    carries none, so the lowest release type WITH a rating is the country's."""
    out = {}
    for block in (payload.get("release_dates") or {}).get("results") or ():
        country = str(block.get("iso_3166_1") or "").upper()
        releases = sorted(block.get("release_dates") or (), key=lambda r: r.get("type") or 0)
        for release in releases:
            rating = str(release.get("certification") or "").strip().upper()
            if rating:
                out.setdefault(country, rating)
                break
    return out


def release_types_by_country(payload: dict) -> dict[str, list[int]]:
    """A film's {country: [release type]} — TMDB's own numbering, which is what
    Record.release_types_by_country holds and the release filter reads."""
    out: dict[str, list[int]] = {}
    for block in (payload.get("release_dates") or {}).get("results") or ():
        country = str(block.get("iso_3166_1") or "").upper()
        kinds = sorted({int(r["type"]) for r in block.get("release_dates") or ()
                        if isinstance(r.get("type"), int)})
        if country and kinds:
            out[country] = kinds
    return out


def country_code(payload: dict) -> str:
    """The country the title comes from, upper-case: its first `origin_country`."""
    countries = payload.get("origin_country") or ()
    return str(countries[0]).upper() if countries else ""


def language_code(payload: dict) -> str:
    """The title's original language, upper-case ISO 639-1 — the form a stored
    record and the language filter use."""
    return str(payload.get("original_language") or "").upper()


def runtime(payload: dict, media: Media) -> int | None:
    """Minutes. A film's own runtime. A show's is its LAST AIRED EPISODE's (else
    its next one's), never `episode_run_time`: measured, that list is empty on
    most shows and stale on others, while the episode TMDB names as the latest
    states what an episode of this show runs now."""
    if media is Media.MOVIE:
        return payload.get("runtime") or None
    for key in ("last_episode_to_air", "next_episode_to_air"):
        if minutes := (payload.get(key) or {}).get("runtime"):
            return int(minutes)
    return None


# A TMDB score from a handful of votes is noise — a title with three votes
# averages anywhere — and the card draws this beside the other services' scores
# as though it meant the same. Below this many votes it is no rating at all.
RATING_VOTE_FLOOR = 10


def rating(payload: dict) -> float | None:
    if int(payload.get("vote_count") or 0) < RATING_VOTE_FLOOR:
        return None
    score = payload.get("vote_average")
    return round(float(score), 1) if score else None


def ids_of(payload: dict) -> dict[str, Any]:
    """Every id this TMDB answer names, in this app's id vocabulary. `tmdb` is
    the title's own id — the id TMDB is asked by, and also the shared id every
    other service states, which is why one key serves both."""
    external = payload.get("external_ids") or {}
    return collect_ids({
        "tmdb": payload.get("id"),
        "imdb": external.get("imdb_id") or payload.get("imdb_id"),
        "tvdb": external.get("tvdb_id"),
    })


def title_of(payload: dict, media: Media) -> str:
    return str(payload.get("name" if media is Media.SHOW else "title") or "").strip()


def first_aired(payload: dict, media: Media) -> str:
    """The title's own first date, YYYY-MM-DD, "" when TMDB has none. A DATE:
    TMDB publishes no air times."""
    return str(payload.get("first_air_date" if media is Media.SHOW else "release_date") or "")


def year_of(payload: dict, media: Media) -> int | str:
    first = first_aired(payload, media)
    return int(first[:4]) if first[:4].isdigit() else ""


def network_of(payload: dict) -> str:
    networks = payload.get("networks") or ()
    return str(networks[0].get("name") or "").strip() if networks else ""


def poster_of(payload: dict) -> str:
    """The title's poster as an ORIGIN address, "" for none."""
    return transport.image_url(payload.get("poster_path"))


# ---------------------------------------------------------------------------
# Videos
# ---------------------------------------------------------------------------

# Where TMDB hosts a video, and how its `key` becomes a page. TMDB lists two
# sites; a third is skipped rather than guessed at, because an offered video
# that goes nowhere is worse than none.
_VIMEO = "https://vimeo.com/"


def videos_from(payload: dict) -> list[Video]:
    """TMDB's `videos.results` as `Video`s, in TMDB's order. A YouTube entry is
    kept only when app/youtube.py reads its key as a video, the strict reading
    every other service's videos go through."""
    out = []
    for entry in (payload.get("videos") or {}).get("results") or ():
        if not isinstance(entry, dict):
            continue
        site = str(entry.get("site") or "").strip().lower()
        key = str(entry.get("key") or "").strip()
        if site == "youtube":
            video_id = youtube.video_id(key) or ""
            if not video_id:
                continue
            url = youtube.watch_url(video_id)
        elif site == "vimeo" and key.isdigit():
            video_id, url = "", f"{_VIMEO}{key}"
        else:
            continue
        official = entry.get("official")
        out.append(Video(
            site=site, video_id=video_id, url=url,
            title=" ".join(str(entry.get("name") or "").split()),
            kind=VideoKind.read(entry.get("type")),
            official=official if isinstance(official, bool) else None,
            published=str(entry.get("published_at") or "") or None,
            sources=(str(Source.TMDB),)))
    return out


# ---------------------------------------------------------------------------
# Seasons and episodes
# ---------------------------------------------------------------------------

def _date(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def season_summary(season_payload: dict, number: int, today: date) -> dict:
    """GET /tv/{id}/season/{n} as app/providers/season.py's summary. Every
    episode counts, dated or not — the shared rule needs the undated ones to tell
    a half-announced season from a finished one. TMDB dates episodes and does not
    time them, so these dates are TMDB's own days with nothing converted."""
    episodes = sorted((e for e in season_payload.get("episodes") or () if isinstance(e, dict)),
                      key=lambda e: e.get("episode_number") or 0)
    if not episodes:
        return season_rules.empty_season(number)
    return {"season": number,
            **season_rules.derive_season([_date(e.get("air_date")) for e in episodes], today)}


# How an episode's day is written in the panel's list; the other services'
# packages use the same strftime so two services' lists read alike.
_AIR_DISPLAY = "%d %b %Y"


def panel_episodes(season_payload: dict) -> list[dict]:
    """The panel's episode rows for one season, in episode order."""
    rows = []
    for entry in season_payload.get("episodes") or ():
        if not isinstance(entry, dict) or entry.get("episode_number") is None:
            continue
        number = int(entry["episode_number"])
        aired = _date(entry.get("air_date"))
        rows.append({
            "number": number,
            "title": str(entry.get("name") or f"Episode {number}"),
            "air_display": aired.strftime(_AIR_DISPLAY) if aired else "",
            "rating": rating(entry),
            "overview": str(entry.get("overview") or "").strip(),
        })
    rows.sort(key=lambda row: row["number"])
    return rows


def picker_seasons(payload: dict) -> list[dict]:
    """A show's seasons as the season picker offers them —
    [{season, episode_count, first_aired, name, poster}] — from the `seasons`
    list `/tv/{id}` already carries. Specials (season 0) and a season holding no
    episodes yet are left out, the rule every service's picker follows."""
    out = []
    for entry in payload.get("seasons") or ():
        number = entry.get("season_number")
        count = int(entry.get("episode_count") or 0)
        if not number or count <= 0:
            continue
        out.append({"season": int(number), "episode_count": count,
                    "first_aired": str(entry.get("air_date") or ""),
                    "name": season_name(entry),
                    "poster": transport.image_url(entry.get("poster_path"))})
    out.sort(key=lambda s: s["season"])
    return out


# "Season 3" restates the number; only a real name is worth drawing. The same
# rule the Trakt package applies to its own season titles.
_NUMBERED_SEASON = re.compile(r"(?i)season\s+\d+")


def season_name(entry: dict) -> str:
    name = " ".join(str(entry.get("name") or "").split())
    return "" if not name or _NUMBERED_SEASON.fullmatch(name) else name


def season_entry(payload: dict, number: int | None) -> dict:
    """One season out of `/tv/{id}`'s `seasons` list, or {}."""
    if number is None:
        return {}
    for entry in payload.get("seasons") or ():
        if entry.get("season_number") == number:
            return entry
    return {}


# ---------------------------------------------------------------------------
# The calendar record
# ---------------------------------------------------------------------------

def title_url(media: Media, ids: dict) -> str:
    """TMDB's own page for a title, "" with no tmdb id. TMDB's terms ask that its
    data link back to TMDB, and a card's outbound button is that link."""
    tmdb = ids.get("tmdb")
    if not tmdb:
        return ""
    return f"https://www.themoviedb.org/{'tv' if media is Media.SHOW else 'movie'}/{tmdb}"


def episode_url(ids: dict, season: int, number: int) -> str:
    """TMDB's page for one episode, "" with no tmdb id."""
    tmdb = ids.get("tmdb")
    if not tmdb:
        return ""
    return f"https://www.themoviedb.org/tv/{tmdb}/season/{season}/episode/{number}"


def to_record(payload: dict, media: Media, season: int | None, when: str) -> Record | None:
    """One airing of the title `payload` describes, as a calendar Record — THE
    record builder for this source, so a record built for a search and one built
    by a fill are the same record.

    `when` is a YYYY-MM-DD day or an ISO instant. TMDB states every date as a
    DAY, so the record is `date_only`: rendered as the day TMDB published,
    never converted into the day before for a viewer west of Greenwich.
    `season` names the season this airing premieres (episode one); None is a
    film or a title placed by its own first date.
    """
    day = str(when or "")[:10]
    try:
        moment = datetime.fromisoformat(f"{day}T00:00:00+00:00")
    except ValueError:
        return None
    ids = ids_of(payload)
    if not ids.get("tmdb"):
        return None
    episode = {}
    if media is Media.SHOW and season is not None:
        episode = {"season": int(season), "episode_number": 1,
                   "episode_label": f"S{int(season):02d}E01"}
    return Record(
        source=Source.TMDB,
        media=media,
        id=str(ids["tmdb"]),
        ids=ids,
        detail_url=title_url(media, ids),
        title=title_of(payload, media) or "Untitled",
        air_ts=epoch_seconds(moment),
        date_only=True,
        year=year_of(payload, media),
        network=network_of(payload) if media is Media.SHOW else "",
        country=country_code(payload),
        language=language_code(payload),
        runtime=runtime(payload, media),
        status=status_word(payload.get("status")),
        rating=rating(payload),
        genres=genre_slugs(payload.get("genres")),
        certification=certification(payload, media),
        overview=str(payload.get("overview") or "").strip(),
        poster=poster_of(payload) or None,
        release_types_by_country=(release_types_by_country(payload)
                                  if media is Media.MOVIE else {}),
        **episode,
    )
