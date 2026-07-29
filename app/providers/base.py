"""The shape every calendar source must speak, and nothing about any one source.

This module is the seam. It declares WHAT a source produces (`Item`), WHAT it is
able to answer (`Capabilities`), and the one method the calendar route calls
(`Provider`). It imports nothing from the rest of the app at runtime, so a
provider implementation can depend on it without anything depending back.

WHY A DATACLASS AND NOT A TypedDict OR A DICT. There is no type checker in this
project's CI, so a TypedDict would document the contract without enforcing it —
and the entire value of this seam is that a second source emits the SAME record
as the first. A dataclass raises at construction, inside that provider's own
tests, the moment a field is forgotten or invented. The templates already use
dot access, which reads identically either way.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:  # import-only-for-annotations: endpoints.py imports Media from here
    from ..config import Settings
    from ..endpoints import Endpoint


class Media(StrEnum):
    """The two kinds of title this app deals in.

    A StrEnum rather than bare strings so the closed set is stated once, while
    each member still IS the string the database columns, the JSON payloads and
    the provider response keys hold — no conversion layer, and no way to store
    `Media.SHOW` by accident.
    """
    SHOW = "show"
    MOVIE = "movie"


def parse_media(value: Any, default: Media | None = None) -> Media:
    """A client-supplied media type, or a raised refusal. `default` applies only
    when the value is absent entirely, never when it is present and wrong."""
    if value in (None, "") and default is not None:
        return default
    try:
        return Media(value)
    except ValueError:
        raise ValueError(f"Unknown media type {value!r}.") from None


class Source(StrEnum):
    """Which service produced a record.

    One member today. It is an enum rather than a bare "trakt" string because
    this value is written into `Item.source` by every provider and read back to
    decide who to ask for a detail lookup — a typo in either half would produce
    a record that silently belongs to nobody.
    """
    TRAKT = "trakt"


# The id namespaces an Item may carry, and the only keys `Item.ids` uses. These
# name an ID SPACE, not a provider: `tmdb` means "this title's id at TMDB",
# which two different sources can both supply and agree on. That is the property
# that lets the same title arriving from two services be recognized as one.
ID_KEYS = ("trakt", "slug", "simkl", "tvdb", "tmdb", "imdb", "mal")


def collect_ids(raw: Mapping[str, Any]) -> dict[str, Any]:
    """The subset of `raw` that ID_KEYS names, with empty values dropped.

    ABSENT KEYS ARE OMITTED RATHER THAN SET TO None, so `"tmdb" in item.ids`
    answers "is this title known to TMDB" without every reader also having to
    check for a None it was handed as a placeholder.
    """
    return {key: raw[key] for key in ID_KEYS if raw.get(key) not in (None, "")}


@dataclass
class Item:
    """One airing on the calendar, as any source must describe it.

    NOT FROZEN: the calendar read path annotates items in place (day layout,
    per-viewer marks) and a frozen record would force a copy at each step for no
    safety anyone is currently relying on.

    Provenance is deliberately three fields rather than one provider's ids
    hoisted to the top level:
      `source`     which service produced THIS record,
      `ids`        every id space it named this title in (see collect_ids),
      `detail_url` the canonical page for it on that service.
    A top-level `trakt_id` would have to be either renamed or duplicated by the
    second source; `ids["trakt"]` does not.
    """
    # Provenance and identity.
    source: Source
    media: Media
    # The stable per-title key the app's own state is filed under: not-watching
    # marks, the is-new diff, the per-day counts. Provider-scoped by nature, so
    # it is whatever that provider can promise is stable for a title.
    id: str
    ids: dict[str, Any]
    detail_url: str

    # When it airs, already converted into the VIEWER's timezone — every one of
    # these is a rendering of the same moment, precomputed because the template
    # cannot do timezone arithmetic and the sort needs the timestamp.
    air_date: str        # YYYY-MM-DD, local
    air_ts: float        # unix seconds; the sort key
    air_display: str     # "03 Jul 2026"
    air_time: str        # "21:00"
    day_of_week: str     # "Friday"

    title: str

    # Everything below is genuinely optional: a source that does not carry it,
    # or a title that has none, leaves it at the default rather than inventing a
    # value the card would then render as fact.
    year: int | str = ""
    network: str = ""
    country: str = ""
    language: str = ""
    runtime: int | None = None
    status: str = ""
    rating: float | None = None
    genres: list[str] = field(default_factory=list)
    certification: str = ""
    overview: str = ""
    poster: str | None = None
    # Episode coordinates. Present on the show endpoints, absent on movies.
    episode_label: str | None = None   # "S02E05"
    episode_title: str = ""
    season: int | None = None
    episode_number: int | None = None


@dataclass(frozen=True)
class Capabilities:
    """What a source can be asked for, so a route can check instead of guessing.

    The alternative is every route growing a chain of if-source tests, which is
    exactly the shape this package exists to avoid: asking "can this source
    answer for this month?" stays one call however many sources are registered.
    """
    # The endpoints.ENDPOINTS keys this source answers. A source that has no
    # equivalent of, say, season finales simply omits that key.
    endpoints: frozenset[str]
    # The window around today this source's calendar reaches, in days, or None
    # for "no known bound". A calendar feed that only publishes a rolling window
    # cannot answer for last year, and the honest answer is better than an empty
    # month that reads as "nothing airs then".
    days_before: int | None
    days_after: int | None
    # Whether this source can reach the signed-in person's own data — watch
    # history, progress, ratings. False means it can populate a calendar but can
    # never back the tracker.
    private_user_data: bool

    def answers(self, endpoint_key: str) -> bool:
        return endpoint_key in self.endpoints

    def covers(self, day: date, *, today: date | None = None) -> bool:
        """Whether `day` falls inside this source's reachable window."""
        anchor = today or date.today()
        if self.days_before is not None and day < anchor - timedelta(days=self.days_before):
            return False
        if self.days_after is not None and day > anchor + timedelta(days=self.days_after):
            return False
        return True


class Provider(Protocol):
    """The whole of what the calendar route needs from a source.

    KEPT THIS NARROW ON PURPOSE. Detail lookups, search and the private sync
    reads are each a different consumer with a different degradation story, and
    folding them in here would mean a source that only publishes a calendar
    could not be registered at all. They become their own protocols when a
    second source actually needs them.
    """
    source: Source
    label: str
    capabilities: Capabilities

    def is_configured(self, settings: Settings) -> bool:
        """Whether this source has the credentials it needs to be asked
        anything. The registry uses it to pick a usable calendar source, so it
        must answer without making a network call."""
        ...

    async def fetch_calendar(self, endpoint: Endpoint, settings: Settings,
                             year: int, month: int) -> list[Item]:
        ...
