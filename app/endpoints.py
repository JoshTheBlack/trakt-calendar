"""Registry of the switchable calendar endpoints.

DELIBERATELY SOURCE-NEUTRAL. An endpoint here is the app's own idea of a
calendar — "season premieres", "movie premieres" — named by a key that reaches
the ?endpoint= query param, the api_cache rows and the per-user view state. How
a particular service is asked for that calendar is the PROVIDER's knowledge:
each one translates this key into its own request (see the Trakt provider's
calendar_path). Keeping the translation there is what lets a second source
answer the same dropdown without the dropdown, the cache keys or anyone's saved
preference having to change.

`media` says which kind of title each entry wraps, which is also the key the
response object hangs under, so the normalizer and the filters can adapt.
"""
from __future__ import annotations

from dataclasses import dataclass

from .providers.base import Media


@dataclass(frozen=True)
class Endpoint:
    key: str            # stable id used in the ?endpoint= param / state filenames
    label: str          # human label shown in the dropdown
    media: Media        # which object each item wraps
    has_episode: bool   # whether items include an "episode" object (SxxEyy)
    description: str
    # THIS CALENDAR'S BIT in the title store's `calendars` masks — which
    # calendars a service lists an episode's instant on, one integer per
    # instant rather than a row per calendar (app/db.py's MIGRATION_49). A
    # STORED MEANING: a bit, once written, names this calendar for as long as
    # the rows holding it exist, so a bit is never reused or renumbered.
    # tests/kernel/test_title_store_migration.py holds them distinct and equal
    # to the ones the store was built with.
    bit: int
    # THE ONE WORD THE CALENDAR'S OWN PICKER SHOWS, and only the picker. The page
    # header has to fit a phone in two rows, and a <select> cannot change its
    # option text by screen width, so the picker says this at every width — the
    # full `label` stays everywhere else a calendar is named (search results,
    # titles, the picker's own tooltip). Empty falls back to the label.
    short: str = ""


# Order here is the order shown in the UI dropdown.
ENDPOINTS: dict[str, Endpoint] = {
    "shows/new": Endpoint(
        key="shows/new",
        bit=1,
        label="Series Premieres",
        media=Media.SHOW,
        has_episode=True,
        description="Brand-new shows premiering their very first episode.",
        short="Series",
    ),
    "shows/premieres": Endpoint(
        key="shows/premieres",
        bit=2,
        label="Season Premieres",
        media=Media.SHOW,
        has_episode=True,
        description="Season premieres (episode 1 of any season).",
        short="Seasons",
    ),
    "shows/finales": Endpoint(
        key="shows/finales",
        bit=4,
        label="Season Finales",
        media=Media.SHOW,
        has_episode=True,
        description="Season/series finales airing this month.",
        short="Finales",
    ),
    "shows": Endpoint(
        key="shows",
        bit=8,
        label="All Episodes",
        media=Media.SHOW,
        has_episode=True,
        description="Every episode of every show airing this month.",
        short="Episodes",
    ),
    "movies": Endpoint(
        key="movies",
        bit=16,
        label="Movie Premieres",
        media=Media.MOVIE,
        has_episode=False,
        description="Theatrical / streaming movie premieres this month.",
        short="Movies",
    ),
}

DEFAULT_ENDPOINT = "shows/new"


def get_endpoint(key: str | None) -> Endpoint:
    """Return the endpoint for `key`, falling back to the default."""
    if key and key in ENDPOINTS:
        return ENDPOINTS[key]
    return ENDPOINTS[DEFAULT_ENDPOINT]


def endpoint_choices() -> list[Endpoint]:
    return list(ENDPOINTS.values())
