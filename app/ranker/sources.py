"""Where titles come from, behind seams the rest of the ranker cannot see past.

The ranker takes titles from three kinds of place: a search, a ratings list, and
(optionally) the tracker. Routes ask this module and get back board items and
`TitleRef`s, which say nothing about which service produced them, so nothing
downstream names a provider.

THE SEARCH IS THE APP'S, NOT THIS FEATURE'S. The board asks exactly what the
calendar's and the tracker's add boxes ask: TYPING reads the titles this instance
already stores (app/calendar/entries.py's titles_matching — the calendar owns
those rows, so it owns the verb), and ENTER asks the services through
app/providers/title_search.py, the one fan-out and merge every searching feature
shares. What is the ranker's is what a chosen result BECOMES — a board item,
named by reference and read back from what a service said (`named_hit`,
`board_item`).

CREDENTIALS ARE NOT THE SAME FOR BOTH SERVICE READS, and getting it backwards is
the easy mistake:
  - SEARCH builds the viewer's settings (auth/viewer.py), which is how "who is
    asked" is answered for every searching feature: Trakt authenticates a
    catalogue search with the instance's own client id, so it answers for every
    account — the rankings grant implies no linked account at all — while Simkl
    searches only with the viewer's OWN token and so only for a viewer who has
    linked one (`providers.for_catalogue_search`). Nobody's search is made with
    the operator's token.
  - RATINGS are private to one person and need THEIR token. With no linked
    identity there is no such token, and the action is hidden rather than
    offered and then failed.
"""
from __future__ import annotations

import dataclasses
import logging
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import item_keys
from .. import providers
from ..auth import trakt_routes, viewer
# The stored-title half of every search. See the module docstring for why the
# ranker reads it through the calendar's verb rather than a query of its own.
from ..calendar import entries as calendar_entries
from ..providers import title_search
from ..providers.trakt import TraktError, detail as trakt_detail, sync as trakt_sync
from ..config import Settings, load_settings
# Re-exported deliberately: `sources.Media` and `sources.parse_media` are what
# the ranker's routes and data layer already import, and the vocabulary
# they name is the app's, not this feature's — the calendar and the tracker deal
# in the same two kinds of title. One definition, read from where it lives.
#
# `resolve_identity` comes from there too, and is CALLED here rather than
# re-exported: the tracker keys its own rows on the same waterfall's answer, so a
# second copy would be two definitions of what makes two titles the same one.
from ..providers.base import Media, parse_media, resolve_identity  # noqa: F401

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TitleRef:
    """One title, said in a way that does not name where it came from.

    Everything downstream of a source — the routes, the data layer, the board —
    sees this and only this, which is what keeps a second provider from being a
    change to anything but a source class.

    `ids` carries the WHOLE map a provider knew, not just the id we matched on:
    an id dropped here is one a future cross-service match cannot use, and it
    costs a call already paid for to get back.
    """
    media: Media
    title: str
    ids: Mapping[str, Any] = field(default_factory=dict)
    year: int | None = None
    network: str = ""
    season_count: int | None = None
    episode_count: int | None = None
    runtime: int | None = None
    # Which season of the show this is, None for the whole show. Part of what the
    # board row IS (its key), so it travels into as_item's mapping beside the
    # identity rather than among the descriptive fields.
    season: int | None = None

    def identity(self) -> tuple[str, str] | None:
        """(match_source, match_id) by the waterfall, or None when this title
        shares no id we can key on."""
        return resolve_identity(self.ids)

    def as_item(self, **extra: Any) -> dict[str, Any] | None:
        """The mapping the board data layer stores, or None for a title with no
        usable id — which cannot be ranked, because nothing could tell it apart
        from another title with the same name.

        `extra` carries the fields only one caller knows, such as the score a
        ratings seed attaches.
        """
        identity = self.identity()
        if identity is None:
            return None
        match_source, match_id = identity
        return {
            "media": str(self.media),
            "match_source": match_source,
            "match_id": match_id,
            "season": self.season,
            # The artwork key specifically, which is tmdb or nothing: TMDB is
            # what serves the image, so a title matched on tvdb still has no
            # poster to fetch and falls back to the placeholder.
            "tmdb": int_or_none(self.ids.get("tmdb")),
            "ids": dict(self.ids),
            "title": self.title,
            "year": self.year,
            "network": self.network,
            "season_count": self.season_count,
            "episode_count": self.episode_count,
            "runtime": self.runtime,
            **extra,
        }


@dataclass(frozen=True)
class RatedTitle:
    """A title with the score its owner gave it."""
    title: TitleRef
    rating: int
    rated_at: str | None = None


def int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def items_from_refs(refs, **extra: Any) -> list[dict[str, Any]]:
    """Every ref that resolved to an identity, in the shape the data layer takes.

    Titles that resolve to nothing are dropped with a log line rather than
    raising: one obscure entry in a search or an import is not a reason to
    refuse the other nineteen.
    """
    items = []
    for ref in refs:
        item = ref.as_item(**extra)
        if item is None:
            logger.info("ranker: dropping %r — no id in the match waterfall", ref.title)
            continue
        items.append(item)
    return items


class SourceUnavailable(RuntimeError):
    """A source could not answer — it was unreachable, refused the credential,
    or replied with something unreadable.

    PROVIDER-NEUTRAL ON PURPOSE. A route that caught the provider's own
    exception type would name that provider, which is the coupling the seams
    exist to prevent; each implementation translates its own failures into this
    on the way out. `status` carries the upstream code when there was one, so a
    401 can be reported as a credential problem rather than a generic outage.
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class NoLinkedIdentity(SourceUnavailable):
    """A per-user provider read was attempted for an account with no linked
    identity. Raised rather than returning empty, because "you have rated
    nothing" and "we could not ask" are different answers."""


# ---------------------------------------------------------------------------
# the seams
# ---------------------------------------------------------------------------

class RatingsSource(Protocol):
    """One person's own scores, read with their own credential."""
    async def fetch_ratings(self, user_id: int) -> list[RatedTitle]: ...


class FinishedTitlesSource(Protocol):
    """Titles a person has finished watching, for the optional import.

    `media` and `year` are part of the seam rather than filters applied to its
    result because only the provider knows what "finished during 2026" means in
    its own data — for the tracker it is the month a completed record belongs
    to, which is not derivable from a TitleRef at all.
    """
    async def finished_titles(
        self, user_id: int, *, media: Media, year: int | None = None,
    ) -> list[TitleRef]: ...


# ---------------------------------------------------------------------------
# the Trakt implementations
# ---------------------------------------------------------------------------

@contextmanager
def _translating_failures():
    """Turn this provider's own failure type into the neutral one, at the edge
    of the implementation and nowhere else."""
    try:
        yield
    except TraktError as exc:
        raise SourceUnavailable(str(exc), getattr(exc, "status", None)) from exc


class TraktRatingsSource:
    """`RatingsSource` over Trakt's /sync/ratings, read with the user's own
    token — never the instance credential, which would hand everybody the
    operator's ratings."""

    async def fetch_ratings(self, user_id: int) -> list[RatedTitle]:
        settings = await user_trakt_settings(user_id)
        if settings is None:
            # The caller is expected to have checked availability first; this is
            # the belt to that braces, so a race between unlinking and clicking
            # cannot read with somebody else's credential.
            raise NoLinkedIdentity("No linked account to read ratings from.")
        with _translating_failures():
            entries = await trakt_sync.fetch_ratings(settings)
        rated = []
        for entry in entries:
            ref = _ref_from_rating(entry)
            score = int_or_none(entry.get("rating"))
            if ref is None or score is None:
                continue
            rated.append(RatedTitle(title=ref, rating=score, rated_at=entry.get("rated_at")))
        return rated


def _ref_from_rating(entry: Mapping[str, Any]) -> TitleRef | None:
    """One Trakt ratings entry as the title a board would hold, or None.

    A SHOW OR A FILM is that title whole. A SEASON is a season row of its show:
    Trakt's entry carries the parent `show` beside `season.number`, and the row
    is keyed on the show's ids with the season beside them, exactly as a board
    keys any season — so a rated season arrives as that season, and over a
    whole-show row it takes that row over (ranker.seed_ratings). An EPISODE is
    not something a board can hold, and nothing else is recognised.
    """
    kind = entry.get("type")
    season = None
    if kind == "season":
        number = (entry.get("season") or {}).get("number")
        season = number if isinstance(number, int) and not isinstance(number, bool) else None
        if season is None:
            return None
        kind = "show"
    elif kind not in ("show", "movie"):
        return None
    item = entry.get(kind) or {}
    ids = trakt_detail.ids_map(item) if isinstance(item, dict) else {}
    if not ids:
        return None
    return TitleRef(
        media=Media(kind),
        title=item.get("title") or "",
        ids=ids,
        year=int_or_none(item.get("year")),
        network=item.get("network") or "",
        runtime=int_or_none(item.get("runtime")),
        season=season,
    )


async def user_trakt_settings(user_id: int) -> Settings | None:
    """The app-wide settings with the Trakt credential swapped for `user_id`'s
    own, or None when they have not linked an account.

    Deliberately not shared with the tracker's equivalent helper: that one exists
    to read somebody's private watch history and answers to the tracker's own
    access level, this one exists to read their ratings and answers to a grant
    that does not imply a linked account at all. The two look alike today; they
    are not the same rule, and merging them would put the tracker's assumption
    (there is always a token) behind a feature where there often is not.
    """
    token = await trakt_routes.access_token_for_user(user_id)
    if not token:
        return None
    return dataclasses.replace(
        load_settings(), trakt_access_token=token, trakt_refresh_token="",
    )


# ---------------------------------------------------------------------------
# what a route asks for
# ---------------------------------------------------------------------------
# Routes call these rather than naming an implementation, which is both the seam
# and what lets a test drive the whole path with a fake source.

async def search_settings(user_id: int) -> Settings:
    """The settings a board search asks the services with — the viewer's own
    (auth/viewer.py), which decides who is asked. See the module docstring."""
    return await viewer.settings_for_user(user_id)


async def typed_search(query: str, media: Media, limit: int) -> list[title_search.MergedSearchHit]:
    """The stored titles matching what is being TYPED, whoever's calendar
    loaded them. UNFILTERED — adding a title is not asking where it is on
    anybody's calendar — and no service is asked and nothing is budgeted: it is
    an index read (app/calendar/entries.py's titles_matching)."""
    return await calendar_entries.titles_matching(query, media, limit)


def too_short(query: str) -> bool:
    """Whether a typed query is still too short to list anything for — the rule
    the stored-title search itself keeps, asked of it rather than restated."""
    return len(calendar_entries.fold_title(query)) < calendar_entries.MIN_QUERY


def searchable(settings: Settings) -> bool:
    """Whether any service can be asked at all with these settings."""
    return bool(providers.for_catalogue_search(settings))


async def catalogue_search(settings: Settings, query: str, media: Media, *,
                           cached_only: bool = False) -> title_search.SearchMergeResult:
    """Enter's search: every service these settings can ask, merged into one
    list, each hit marked with who found it. The shared fan-out stores each
    service's answer, which is what lets a pick be read back later by its place
    in this list (`named_hit`). `cached_only` re-reads that stored answer and
    asks nobody — how a page answering an add redraws the search it came from.
    """
    return await title_search.search_catalogue(
        providers.for_catalogue_search(settings), settings, media, query,
        cached_only=cached_only)


async def named_hit(settings: Settings, media: Media,
                    named: Mapping[str, Any]) -> title_search.MergedSearchHit | None:
    """The result a request NAMES, read back from what a service said, or None
    when nothing this instance holds answers to that name.

    TWO WAYS OF NAMING, ONE PER HALF OF THE SEARCH, and neither carries a title:
    a stored title by the identity it is stored under (`t`, a
    calendar_titles.title_key — app/calendar/entries.py's stored_title), and a
    service's hit by the query that found it and its place in that answer (`q`
    and `i` — title_search.resolve_hit, which re-reads the stored search and
    asks nobody). So what a board stores is what a service said, never fields a
    page handed back, and a key this instance does not hold is a refusal.
    """
    title_key = str(named.get("t") or "")
    if title_key:
        return await calendar_entries.stored_title(title_key, media)
    query = str(named.get("q") or "").strip()
    if not query:
        return None
    return await title_search.resolve_hit(settings, media, query, named.get("i"))


def gone_sentence(named: Mapping[str, Any]) -> str:
    """What to say when `named_hit` finds nothing: which way the result was
    named says what went away, and so what to do about it."""
    if named.get("t"):
        return "That title is no longer stored here — search for it again."
    return "That search has expired — search for it again."


def hit_key(hit: title_search.MergedSearchHit,
            looked_up: Mapping[str, Any] | None = None) -> str | None:
    """The board key of the WHOLE title a hit is, or None for a hit that shares
    no id the identity waterfall keys on (a service's season-title before its
    per-title lookup). It is how a result is drawn as already held, and what a
    dragged result's row is inserted under. `looked_up` is the ids a season
    lookup surfaced, filling what the hit left out — the same union
    `board_item` stores."""
    identity = resolve_identity({**(looked_up or {}), **hit.ids})
    if identity is None:
        return None
    return item_keys.board_key(str(hit.media), *identity)


async def season_pick(settings: Settings,
                      hit: title_search.MergedSearchHit) -> title_search.Pick | None:
    """A show hit's season list — the per-title lookup a result's checklist is
    opened for — plus every id it surfaces and the season a season-title names.
    The shared picker verb, so a title's seasons read the same here as in the
    tracker's add."""
    return await title_search.pick_from_hit(settings, hit, cached_only=False)


async def board_item(settings: Settings,
                     hit: title_search.MergedSearchHit) -> dict[str, Any] | None:
    """The whole title a hit names, as the board data layer stores it, or None
    for one with no id a board can key it on.

    A HIT THE SEARCH LEFT KEYLESS IS READ AGAIN THROUGH ITS SEASON LOOKUP, cache
    only: a service that files each season as a title of its own often answers a
    search with no shared id, and opening the result's season list is what
    fetched the ids that key it. Never a request here — an add that could look
    something up would be a search nobody counted.
    """
    ids = dict(hit.ids)
    network = hit.network
    if resolve_identity(ids) is None and hit.media is Media.SHOW:
        pick = await title_search.pick_from_hit(settings, hit, cached_only=True)
        if pick is not None:
            ids = {**pick.ids, **ids}
            network = network or pick.network
    return TitleRef(media=hit.media, title=hit.title, ids=ids, year=hit.year,
                    network=network or "", runtime=hit.runtime).as_item()


def ratings_source() -> RatingsSource:
    """The ratings seam. Resolves the caller's own credential when asked, so
    availability and use cannot disagree about whose token is in play."""
    return TraktRatingsSource()


async def ratings_available(user_id: int) -> bool:
    """Whether to OFFER a ratings seed at all. False hides the action rather
    than showing one that fails: an account with no linked identity has nothing
    to seed from, and a disabled button it can never enable is just noise."""
    return bool(await trakt_routes.access_token_for_user(user_id))
