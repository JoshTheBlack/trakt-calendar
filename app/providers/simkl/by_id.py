"""Simkl's answer to "which of YOUR titles is this shared id?" — the half of
app/providers/base.py's SearchPort that is not a free-text search.

`/redirect`, NOT `/search/id`. It takes every id in MATCH_SOURCES (tmdb with a
`type`, tvdb, imdb, mal) and answers with a 301 whose Location is
`https://simkl.com/{tv|anime|movies}/{simkl id}/{slug}` — the id AND Simkl's own
name for the title in one reply, with no body to parse. The endpoint also offers
a title/year fallback, which this NEVER sends: an answer from a title search is a
guess, and a wrong Simkl id filed on a tracker row is worse than none.

THE ANSWER IS CACHED UNDER THIS MODULE'S OWN KEY, because the response has no
body for the shared response cache to store — the EXTRACTED ids are what is kept,
and "not listed" is kept too. That entry's age is the re-ask clock: a catalogue
that has not listed a title today will not have listed it within the hour, so the
same question is not put to Simkl again for a day.
"""
from __future__ import annotations

import re

from ... import cache
from ...config import Settings
from ..base import MATCH_SOURCES, Media
from . import transport

# How long an answer — including "not listed" — stands before Simkl is asked
# again. A day: short enough that a title catalogued today links tomorrow, long
# enough that repeated adds of an unlisted title do not each cost a request.
REASK_SECONDS = 24 * 60 * 60

# The Location a found title redirects to. The first segment is Simkl's catalogue
# for the title, and it is checked against the media asked for, so a tmdb id that
# names a film in Simkl's catalogue is never filed on a show.
_LOCATION = re.compile(
    r"^https?://(?:www\.)?simkl\.com/(?P<kind>tv|anime|movies)/(?P<id>\d+)(?:/(?P<slug>[^/?#]+))?")
_KINDS = {Media.SHOW: ("tv", "anime"), Media.MOVIE: ("movies",)}
_TMDB_TYPE = {Media.SHOW: "tv", Media.MOVIE: "movie"}


def ids_from_location(location: str, media: Media) -> dict:
    """{"simkl": id, "simkl_slug": slug} from a /redirect Location, or {} when
    it names no title of this media. Pure, so the one piece of parsing here can
    be tested against the shapes Simkl answers with."""
    found = _LOCATION.match(location or "")
    if found is None or found["kind"] not in _KINDS[Media(media)]:
        return {}
    out = {"simkl": int(found["id"])}
    if found["slug"]:
        out["simkl_slug"] = found["slug"]
    return out


async def find_by_shared_id(settings: Settings, media: Media, ids: dict) -> dict:
    """Simkl's own ids for the title `ids` names, by the first shared id Simkl
    recognises, in MATCH_SOURCES order — {} when none of them is listed.

    EVERY SHARED ID IS TRIED, not only the one the caller files the title under:
    a title Simkl does not map to its tmdb id may still be found by its imdb one,
    and each is an exact lookup on an id, never a search by name.

    Raises SimklError when Simkl could not be asked, which the caller must tell
    apart from {} — "not listed" is re-asked tomorrow, "could not ask" should
    not be remembered as an answer at all.
    """
    media = Media(media)
    for id_type in MATCH_SOURCES:
        value = ids.get(id_type)
        if value in (None, "", 0):
            continue
        key = f"simkl_by_id:{media}:{id_type}:{value}"
        known = await cache.get(key, REASK_SECONDS)
        if known is None:
            params = {"to": "simkl", id_type: value}
            if id_type == "tmdb":
                params["type"] = _TMDB_TYPE[media]
            known = ids_from_location(
                await transport.redirect_target(settings, "redirect", params), media)
            await cache.set(key, known)
        if known:
            return dict(known)
    return {}
