"""Registry of the switchable Trakt calendar endpoints (requirement D).

Each endpoint knows how to build its Trakt API path and which media key its
response items carry ("show" or "movie"), so the normalizer can adapt.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Endpoint:
    key: str            # stable id used in the ?endpoint= param / state filenames
    label: str          # human label shown in the dropdown
    path: str           # Trakt path segment after /calendars/all/
    media: str          # "show" or "movie" — which object each item wraps
    has_episode: bool   # whether items include an "episode" object (SxxEyy)
    description: str


# Order here is the order shown in the UI dropdown.
ENDPOINTS: dict[str, Endpoint] = {
    "shows/new": Endpoint(
        key="shows/new",
        label="New Shows (series premieres)",
        path="shows/new",
        media="show",
        has_episode=True,
        description="Brand-new shows premiering their very first episode.",
    ),
    "shows/premieres": Endpoint(
        key="shows/premieres",
        label="Season Premieres",
        path="shows/premieres",
        media="show",
        has_episode=True,
        description="Season premieres (episode 1 of any season).",
    ),
    "shows/finales": Endpoint(
        key="shows/finales",
        label="Season Finales",
        path="shows/finales",
        media="show",
        has_episode=True,
        description="Season/series finales airing this month.",
    ),
    "shows": Endpoint(
        key="shows",
        label="All Episodes Airing",
        path="shows",
        media="show",
        has_episode=True,
        description="Every episode of every show airing this month.",
    ),
    "movies": Endpoint(
        key="movies",
        label="Movie Premieres",
        path="movies",
        media="movie",
        has_episode=False,
        description="Theatrical / streaming movie premieres this month.",
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
