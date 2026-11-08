"""Where titles come from, behind seams the rest of the ranker cannot see past.

The ranker takes titles from three kinds of place: a search, a ratings list, and
(optionally) the tracker. Routes ask this module and get back board items and
`TitleRef`s, which say nothing about which service produced them, so nothing
downstream names a provider.

THE SEARCH IS THE APP'S, NOT THIS FEATURE'S. The board asks exactly what the
calendar's and the tracker's add boxes ask: TYPING reads the titles this instance
already stores (app/metadata/typed_search.py's titles_matching — the title store
owns those rows, so it owns the verb), and ENTER asks the services through
app/providers/title_search.py, the one fan-out and merge every searching feature
shares. What is the ranker's is what a chosen result BECOMES — a board item,
named by reference and read back from what a service said (`named_hit`,
`board_item`), and registered with the title store, which is what teaches it
every service's id.

CREDENTIALS ARE NOT THE SAME FOR BOTH SERVICE READS, and getting it backwards is
the easy mistake:
  - SEARCH builds the viewer's settings (auth/viewer.py), which is how "who is
    asked" is answered for every searching feature (`providers.for_search`): the
    catalogue searched by default, with the instance's own credential, for every
    account — the rankings grant implies no linked account at all — and a service
    the viewer has linked only when they add it, with their own token. Nobody's
    search is made with the operator's token.
  - RATINGS are private to one person and need THEIR token. With no linked
    identity there is no such token, and the action is hidden rather than
    offered and then failed. A seed reads ONE service's ratings, the one the
    viewer pressed for (`ratings_source`), among the services they have linked
    that keep ratings (`ratings_services`).
"""
from __future__ import annotations

import dataclasses
import logging
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import item_keys
from .. import metadata, providers
from ..auth import trakt_routes, viewer
# The stored-title half of every search. See the module docstring for why the
# ranker reads it through the title store's verb rather than a query of its own.
from ..metadata import typed_search as stored_search
from ..providers import title_search
from ..providers.tmdb import account as tmdb_account, transport as tmdb_transport
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
        await load_settings(), trakt_access_token=token, trakt_refresh_token="",
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
    an index read (app/metadata/typed_search.py's titles_matching)."""
    return await stored_search.titles_matching(query, media, limit)


def too_short(query: str) -> bool:
    """Whether a typed query is still too short to list anything for — the rule
    the stored-title search itself keeps, asked of it rather than restated."""
    return stored_search.too_short(query)


async def catalogue_search(settings: Settings, media: Media, query: str,
                           ref: title_search.SearchRef, *,
                           cached_only: bool = False) -> title_search.SearchOutcome:
    """Enter's search, and every widening of it: the services `ref` names for
    this viewer, merged into one list, each hit marked with who found it — the
    one search every box makes (title_search.search). Raises
    providers.UnknownSearchService for a service this viewer may not add. The
    shared fan-out stores each service's answer, which is what lets a pick be
    read back later by its place in this list (`named_hit`); `cached_only`
    re-reads that and asks nobody — how a page answering an add redraws the
    search it came from."""
    return await title_search.search(settings, media, query, ref, cached_only=cached_only)


async def named_hit(settings: Settings, media: Media,
                    named: Mapping[str, Any]) -> title_search.MergedSearchHit | None:
    """The result a request NAMES, read back from what a service said, or None
    when nothing this instance holds answers to that name.

    TWO WAYS OF NAMING, ONE PER HALF OF THE SEARCH, and neither carries a title:
    a stored title by the identity it is stored under (`t`, a title key —
    app/metadata/typed_search.py's stored_title), and a
    service's hit by the query that found it, the search's own reference (the
    services added and the pages shown, title_search.SearchRef) and its place
    in that answer (`q`, `also`, `pages`, `i` — title_search.resolve_hit, which
    re-reads the stored search and asks nobody). So what a board stores is what a
    service said, never fields a page handed back, and a key this instance does
    not hold is a refusal.
    """
    title_key = str(named.get("t") or "")
    if title_key:
        return await stored_search.stored_title(title_key, media)
    query = str(named.get("q") or "").strip()
    if not query:
        return None
    return await title_search.resolve_hit(settings, media, query, named.get("i"),
                                          title_search.SearchRef.named(named))


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
    tracker's add — the season it names as the title store holds it
    (metadata.picked)."""
    return await metadata.picked(await title_search.pick_from_hit(settings, hit,
                                                                  cached_only=False))


async def stored_seasons(settings: Settings,
                         hit: title_search.MergedSearchHit) -> list[dict]:
    """The season list a hit's checklist was drawn from, read back from the
    cache — never a request, for board_item's reason. [] when nothing is stored.
    What a ticked season's episode total and name are read from."""
    pick = await title_search.pick_from_hit(settings, hit, cached_only=True)
    return list(pick.seasons) if pick is not None else []


async def board_item(settings: Settings,
                     hit: title_search.MergedSearchHit) -> dict[str, Any] | None:
    """The whole title a hit names, as the board data layer stores it, or None
    for one with no id a board can key it on.

    A HIT THE SEARCH LEFT KEYLESS IS READ AGAIN THROUGH ITS SEASON LOOKUP: a
    service that files each season as a title of its own often answers a search
    with no shared id, and the per-title lookup is what finds the ids that key it.
    From the store when its "Seasons…" list was opened already, and otherwise
    MADE HERE — the one request an add may make, and only for such a hit. It was
    cache-only, and the add then refused a title it could have held: MEASURED
    live across five searches, all nine keyless results got a tvdb or mal id from
    this lookup, both titles the owner was refused among them. It is the lookup
    opening "Seasons…" makes, which is not a search and is not counted as one;
    raises SourceUnavailable when the service cannot be asked.

    "NOTHING STORED" IS NOT ONLY None: with no lookup stored, the store-only pick
    still answers, carrying the hit's own ids and so keying nothing — measured,
    that is what made the add refuse all four keyless "attack on titan" results.
    So the lookup is made whenever the stored answer cannot key the title.

    EVERY SERVICE'S ID IS LEARNED HERE, through the title store (metadata.ensure,
    with the viewer's settings — the one place the app asks a service by shared
    id). A title found through TMDB's search arrives with its tmdb id alone, and
    a board row holding only that offered no Trakt or Simkl button on its details
    panel although both services list the title; asked here, each service the
    title has no id from is asked once, its answer held for a day by its port.
    The row is filed under the key the store answers, so a title already on the
    board under it is recognised as held.
    """
    ids = dict(hit.ids)
    network = hit.network
    if resolve_identity(ids) is None and hit.media is Media.SHOW:
        pick = await title_search.pick_from_hit(settings, hit, cached_only=True)
        if pick is None or resolve_identity({**pick.ids, **ids}) is None:
            pick = await title_search.pick_from_hit(settings, hit, cached_only=False) or pick
        if pick is not None:
            ids = {**pick.ids, **ids}
            network = network or pick.network
    item = TitleRef(media=hit.media, title=hit.title, ids=ids, year=hit.year,
                    network=network or "", runtime=hit.runtime).as_item()
    if item is None:
        return None
    known = await metadata.ensure(hit.media, ids, settings=settings)
    if known is None or known.triple is None:
        return item
    learned = {**known.ids, **ids}
    return {**item, "match_source": known.triple.match_source,
            "match_id": known.triple.match_id, "ids": learned,
            "tmdb": int_or_none(learned.get("tmdb"))}


# ---------------------------------------------------------------------------
# which service's ratings
# ---------------------------------------------------------------------------
# A SEED IS FROM ONE SERVICE, NAMED BY THE VIEWER. Two services' scores for one
# title are two opinions on two scales, and a seed that mixed them — or quietly
# read whichever service happened to be linked — would arrange a board by numbers
# the viewer never chose between. So the control offers one press per service the
# viewer has linked that keeps ratings, each naming its service, and a name this
# table does not hold, or one the viewer has not linked, is refused.

class UnknownRatingsService(ValueError):
    """A seed named a service that has no ratings this viewer can read — not in
    the table, keeping no ratings, or not linked. A REFUSAL, never read as some
    other service."""


class TmdbRatingsSource:
    """`RatingsSource` over the viewer's own TMDB account (its rated shows and
    films, app/providers/tmdb/account.py), read with THEIR TMDB grant.

    TMDB SCORES IN HALF POINTS, 0.5 TO 10; a board row's rating is a whole number
    on Trakt's 1-10 scale, which its tiers are drawn from (ranker.tier_for_rating).
    So a half point rounds UP — 8.5 seeds as 9, 0.5 as 1 — the nearer whole score
    a person who gave "eight and a half" would have picked between eight and nine.
    TMDB rates a show or a film, never a season, so every title arrives whole.
    """

    async def fetch_ratings(self, user_id: int) -> list[RatedTitle]:
        settings = await viewer.settings_for_user(user_id)
        if not settings.tmdb_account_configured:
            raise NoLinkedIdentity("No linked TMDB account to read ratings from.")
        try:
            rated = await tmdb_account.ratings(settings)
        except tmdb_transport.TmdbError as exc:
            raise SourceUnavailable(str(exc), getattr(exc, "status", None)) from exc
        out = []
        for media, item in rated:
            ref = _ref_from_tmdb(media, item)
            score = _whole_score((item.get("account_rating") or {}).get("value"))
            if ref is None or score is None:
                continue
            out.append(RatedTitle(title=ref, rating=score,
                                  rated_at=(item.get("account_rating") or {}).get("created_at")))
        return out


def _whole_score(value: Any) -> int | None:
    """A TMDB score (0.5-10, half points) as a board's whole 1-10 one, a half
    rounding up (see TmdbRatingsSource). None for anything that is not a score."""
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if not 0 < score <= 10:
        return None
    return max(1, min(10, int(score + 0.5)))


def _ref_from_tmdb(media: Media, item: Mapping[str, Any]) -> TitleRef | None:
    """One rated TMDB item as the title a board would hold. TMDB's own `id` is
    the title's tmdb id — the only id the item carries; the title store teaches
    the rest when the seed registers it."""
    tmdb = int_or_none(item.get("id"))
    if tmdb is None:
        return None
    dated = item.get("first_air_date") if media is Media.SHOW else item.get("release_date")
    return TitleRef(
        media=media,
        title=str(item.get("name") or item.get("title") or ""),
        ids={"tmdb": tmdb},
        year=int_or_none(str(dated or "")[:4]),
    )


# EVERY REGISTERED SERVICE HAS AN ENTRY, and a test says so: a service gaining
# ratings is a decision written here, never one this table quietly lacks.
#   tmdb   the viewer's rated shows and films on TMDB.
#   trakt  the viewer's /sync/ratings (shows, seasons, films).
#   simkl  None: this app reads no Simkl ratings.
_RATINGS: dict[str, RatingsSource | None] = {
    "tmdb": TmdbRatingsSource(),
    "trakt": TraktRatingsSource(),
    "simkl": None,
}


def has_ratings(service: str) -> bool:
    """Whether `service` keeps ratings this app reads. Raises KeyError for a
    name the table does not hold — the table is the registry's, whole."""
    return _RATINGS[service] is not None


async def ratings_services(user_id: int) -> list[tuple[str, str]]:
    """(service, label) for every service this viewer has LINKED that keeps
    ratings, in the declared order — what the seed control offers, one press
    each. Linked means a usable grant on the viewer's own Settings
    (`is_configured`, the private question), so an expired or revoked link is
    not offered."""
    settings = await viewer.settings_for_user(user_id)
    return [(str(source), provider.label)
            for source, provider in providers.registered().items()
            if _RATINGS.get(str(source)) is not None and provider.is_configured(settings)]


async def ratings_source(user_id: int, service: str) -> RatingsSource:
    """The ratings seam for the service a seed NAMED. Raises
    UnknownRatingsService for one the viewer cannot seed from — never another
    service in its place."""
    offered = {name for name, _label in await ratings_services(user_id)}
    source = _RATINGS.get(str(service))
    if source is None or str(service) not in offered:
        raise UnknownRatingsService(f"There are no {service or 'such'} ratings to seed from.")
    return source


async def ratings_available(user_id: int) -> bool:
    """Whether to OFFER a ratings seed at all. False hides the action rather
    than showing one that fails: an account with no linked service that keeps
    ratings has nothing to seed from, and a disabled button it can never enable
    is just noise."""
    return bool(await ratings_services(user_id))
