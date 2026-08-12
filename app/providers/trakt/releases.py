"""Where a film is released, according to Trakt.

WHY THIS EXISTS. Trakt's calendar payload says nothing about how or where a film
is released — a record arrives with a PRODUCTION country and no release schedule
at all — so the films calendar's release filter (app/calendar/filter.py) could
never judge a title Trakt listed. It kept them instead, which is the honest
answer for a title nothing can judge, but on a merged film it meant one record's
silence outvoting the other record's data: the filter could not drop any film
Trakt also listed. Trakt's per-title endpoint carries the answer, so the app
stops having to guess.

MEASURED BEFORE IT WAS BUILT, live against three titles on 2026-08-11:
`/movies/{id}/releases` returns one row per release, each carrying `country`,
`release_type` and `release_date` — 7 rows for one film, 28 for another, 54 for
a third. For a film both services listed, Trakt named the SAME SIX countries
Simkl's own enrichment holds, so the two services agree about a release schedule
and a Trakt-derived map does not fight a Simkl one.

THE SHAPE IT PRODUCES IS THE SHAPE THE FILTER ALREADY READS — {COUNTRY: [type]}
with TMDB's numeric types — because there is one release rule and it is not
learning a second vocabulary. Trakt spells its types as words and this module is
where that is translated, which is the right side of the boundary: a provider
package speaks its own service's language and hands the app the app's.

THE DATES ARE DROPPED, for the reason app/providers/simkl/titles.py's
`_release_types_by_country` sets out at length: the app uses this to decide WHICH
TITLES a viewer sees, deciding which DATE a card is drawn on is a different
feature, and a release outside the stored window could not move a card anyway.

EVERY CALL GOES THROUGH transport.cached_get, which is not a convenience. That
is where Trakt's outbound gate lives — a semaphore sized under the connection
pool — along with the 429 retry/backoff loop and its wall-clock budget. A caller
reaching for httpx directly would pace nothing and would be the first thing to
trip a rate limit on a fan-out. The read is PUBLIC catalogue data, so it is
cached un-privately: a release schedule does not depend on whose token asked,
and one title several calendar windows reference costs one call.
"""
from __future__ import annotations

from typing import Any

from ...config import Settings
from . import transport

# A release schedule changes when a distributor announces something, which is
# neither often nor never. A week keeps a busy month's worth of films to one
# call each while still letting a newly-announced digital date arrive without
# anybody clearing anything.
RELEASES_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60

# Trakt's words for TMDB's numbers. The filter speaks numbers — one release
# vocabulary for both services (app/calendar/filter.py's parse_release_type_spec
# and the Filters panel's chip row are both written in them) — and this is the
# one place the two spellings meet.
#
# TRAKT DOCUMENTS EXACTLY THESE SIX and they line up with TMDB's 1-6 in order,
# which is not a coincidence: Trakt's release types are TMDB's, named. An
# unrecognised word is DROPPED rather than guessed at, because a wrong number
# here would put a film in a filter it does not belong in, while a missing one
# only leaves that release unfilterable — and the country block it came from is
# still kept if any of its other releases translated.
_TYPE_NUMBERS = {
    "premiere": 1,
    "limited": 2,
    "theatrical": 3,
    "digital": 4,
    "physical": 5,
    "tv": 6,
}


def release_types_by_country(rows: Any) -> dict[str, list[int]]:
    """Trakt's release rows reduced to {COUNTRY: [type]}, the filter's shape.

    Country codes are upper-cased and types de-duplicated and sorted, so one
    film's map is one value however Trakt happened to order the rows — the same
    normalising the Simkl side does, and what lets the two be compared at all.

    A row with no country, or whose type is a word this app does not know, adds
    nothing; a country whose every row was unreadable does not appear, because an
    empty list would claim the film has a release there with no format, which is
    a different statement from "nothing known".
    """
    out: dict[str, list[int]] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        country = str(row.get("country") or "").strip().upper()
        number = _TYPE_NUMBERS.get(str(row.get("release_type") or "").strip().lower())
        if not country or number is None:
            continue
        types = out.setdefault(country, [])
        if number not in types:
            types.append(number)
    return {country: sorted(types) for country, types in out.items() if types}


async def fetch_releases(settings: Settings, trakt_id: Any,
                         *, cache_only: bool = False) -> dict[str, list[int]] | None:
    """One film's release map, or None when Trakt could not answer.

    None covers a real failure and an id Trakt does not recognise alike, which
    the drain treats the same way — record the attempt and back off — for the
    reason the Simkl side gives: the status code does not reliably tell the two
    apart, and both mean "do not ask again yet".

    AN EMPTY MAP IS NOT None. A film Trakt knows and has no announced releases
    for answers with an empty list, and that is a real answer worth storing: it
    stops the title being re-queued for ever, and the filter reads an empty map
    as "cannot judge" exactly as it does for an unenriched record.

    `cache_only=True` makes no outbound call, which is what a public share
    surface would read with — no visitor's click may spend the instance's Trakt
    budget.
    """
    if not trakt_id:
        return None
    try:
        payload = await transport.cached_get(
            transport.shared_client(), settings, f"movies/{trakt_id}/releases", {},
            ttl_seconds=RELEASES_CACHE_TTL_SECONDS, cache_only=cache_only,
            raise_errors=True,
        )
    except transport.TraktError:
        return None
    if not isinstance(payload, list):
        return None
    return release_types_by_country(payload)
