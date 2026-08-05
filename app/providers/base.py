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
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol, runtime_checkable

if TYPE_CHECKING:  # import-only-for-annotations: endpoints.py imports Media from here
    from ..config import Settings


class SourceUnavailable(Exception):
    """A source could not answer. THE app-wide degradation contract, stated once
    and named by nobody in particular.

    Each source's own error type derives from this — `TraktError`,
    `SimklError` — so a caller that genuinely wants to handle one service's
    failure specifically still can, while a caller that reads TWO sources and has
    to carry on with whichever answered has something to catch that does not name
    either of them. Without it, the tracker would need one `except` clause per
    registered source, which is exactly the shape a registry exists to prevent.

    `status` is the HTTP status where there was one, and None where the failure
    never got that far.
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


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

    An enum rather than a bare string because this value is written into
    `Item.source` by every provider and read back to decide who to ask for a
    detail lookup — a typo in either half would produce a record that silently
    belongs to nobody. With two members that is no longer hypothetical: the same
    title can arrive from both, and which one a given field came from is a fact
    the reader has to be able to state.
    """
    TRAKT = "trakt"
    SIMKL = "simkl"


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


# ---------------------------------------------------------------------------
# Cross-source title identity
# ---------------------------------------------------------------------------
# The identity waterfall, first shared non-empty id wins. tmdb leads because it
# is the id both Trakt and Simkl expose AND the one that indexes TMDB artwork;
# tvdb is strong for TV, imdb is near-universal but weakest to match on, and mal
# is often the only id two services share for anime.
#
# THIS IS A SUBSET OF ID_KEYS, DELIBERATELY. `trakt`, `slug` and `simkl` are the
# ids you need to CALL a service, and they are the ones a second service does not
# have — so a row keyed on one of them would be two rows for one title the moment
# the same title arrived from somewhere else. What is left is the ids that name a
# title in a space nobody here owns, which is what makes them safe to key on.
MATCH_SOURCES = ("tmdb", "tvdb", "imdb", "mal")


def resolve_identity(ids: Mapping[str, Any]) -> tuple[str, str] | None:
    """THE identity waterfall: (match_source, match_id) for the first of
    MATCH_SOURCES this title is actually known in, or None when it is known in
    none of them and so cannot be told apart from another title of the same name.

    The id is stringified because imdb ids are not numbers and a column holding
    both has to hold text.
    """
    for source in MATCH_SOURCES:
        value = ids.get(source)
        if value not in (None, "", 0):
            return source, str(value)
    return None


@dataclass(frozen=True)
class ItemKey:
    """One title's identity, said without naming who told us about it.

    The same triple two of this app's features key their rows on, and the reason
    they can agree about what "the same title" means: whichever shared id space
    the waterfall landed in, plus the id in it. `str()` gives the flat
    "{media}:{match_source}:{match_id}" form used where one key has to travel as
    a single string — a dict key, an HTML id, a client-supplied item reference.
    """
    media: str
    match_source: str
    match_id: str

    def __str__(self) -> str:
        return f"{self.media}:{self.match_source}:{self.match_id}"


def item_key(media: str, match_source: str, match_id: str) -> str:
    """The flat string form of an ItemKey."""
    return str(ItemKey(media, match_source, match_id))


def parse_item_key(value: Any) -> ItemKey:
    """Parse a flat item key, raising rather than returning a sentinel a caller
    could forget to check.

    Split at most twice, because an imdb or mal id is opaque to us and may one day
    contain the separator; media and match_source never can, since both come from
    closed sets checked here.

    RAISES ValueError, not any one feature's refusal type: the two features that
    parse these keys answer a bad one differently (a board 400s with its own error
    class, the tracker with its own), and the parsing is the same either way.
    """
    if not isinstance(value, str):
        raise ValueError("Item keys must be strings.")
    parts = value.split(":", 2)
    if len(parts) != 3:
        raise ValueError(f"Malformed item key: {value!r}.")
    media, match_source, match_id = parts
    if media not in frozenset(Media):
        raise ValueError(f"Unknown media type {media!r}.")
    if match_source not in MATCH_SOURCES:
        raise ValueError(f"Unknown match source {match_source!r}.")
    if not match_id:
        raise ValueError("An item key needs a match id.")
    return ItemKey(media, match_source, match_id)


def resolve_key(media: Media | str, ids: Mapping[str, Any]) -> ItemKey | None:
    """The ItemKey for a title known by `ids`, or None when the waterfall found
    nothing to key on. Pairs the waterfall with the media type, because a TMDB id
    is namespaced per media kind — movie 550 and TV 550 are different titles."""
    identity = resolve_identity(ids)
    if identity is None:
        return None
    match_source, match_id = identity
    return ItemKey(str(media), match_source, match_id)


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


# runtime_checkable so the suite can isinstance-check a registered source against
# these declarations. It buys a MEMBER-PRESENCE check and nothing more — see
# tests/providers/test_protocol_conformance.py, which states that limit — but
# member presence is the direction that fails in production: adding a verb here
# and forgetting one source is an AttributeError on whichever source the user
# happens to have configured. Nothing in the app calls isinstance on these; the
# structural typing is still what production relies on.
@runtime_checkable
class SyncPort(Protocol):
    """The private, per-person reads a source must answer before it can back the
    tracker: what somebody has watched, when they watched it, and a cheap way to
    tell whether any of that has changed since last time.

    ITS OWN PROTOCOL, not part of `Provider`, because the consumer and the
    failure story are different: the calendar degrades to "this month could not
    be read", while a tracker that cannot read your history has nothing to count
    at all. A source that only publishes a calendar implements none of this and
    says so by declaring `private_user_data=False`.

    THE SHOW ID THESE TAKE IS THE SOURCE'S OWN — the value a roster row carries
    as `trakt_id`, and what a second source would carry as `simkl_id`. It is
    deliberately NOT the shared match id the row is keyed on: you need the
    source's id to place the call and the shared id to file the answer, and
    conflating them is what makes a tracker single-source.

    The event and candidate shapes below are still the source's own payloads
    rather than a normalized record. Normalizing them is worth doing when there
    is a second set to normalize against; inventing the neutral shape from one
    example would be guessing.
    """

    async def fetch_last_activities(self, settings: Settings) -> dict:
        """A small, fixed-size "last changed at" blob, independent of library
        size, that a sync can gate on so an unchanged history costs one call.

        THE SHAPE IS DECLARED HERE, and it is the one thing in this protocol that
        is not simply passed through:

            {"episodes": {"watched_at": <stamp|None>, "removed_at": <stamp|None>},
             "movies":   {"watched_at": <stamp|None>, "removed_at": <stamp|None>}}

        The gate compares the whole blob for equality and watches `removed_at`
        separately, because a removal is not a play and never appears in the
        history — so when one moves, cached progress has to be re-baselined
        rather than folded forward. Extra keys are ignored; a source that files
        its lists differently maps them onto these four AT ITS OWN BOUNDARY. It
        has to live here rather than in the one module that reads it, because
        every source has to uphold it and a rule written inside one reader is a
        claim the other implementers never see.
        """
        ...

    async def fetch_history(self, settings: Settings, start_at: str | None = None) -> list[dict]:
        """This person's watch EVENTS, optionally only those since `start_at`
        (YYYY-MM-DD). Re-seeing an event already applied must be harmless."""
        ...

    async def fetch_progress_details(self, settings: Settings,
                                     show_ids) -> dict[int, dict[int, dict[int, str]]]:
        """{show_id: {season: {episode: watched_at}}} for several shows at once.

        A batch call rather than one-per-show because pooling the connections is
        the source's business, not its caller's — the tracker baselines a whole
        roster at a time and should not have to hold a client to do it.

        AN ID THAT WAS ANSWERED ABOUT IS PRESENT; AN ID THAT COULD NOT BE READ IS
        ABSENT. That is the one thing this shape has to say beyond the counts, and
        it has to be said here because every implementation has to uphold it. An
        empty map against an id is a real answer — this person has seen none of
        that show — and the caller acts on it by retiring the seasons it had
        stored. A show whose own request failed has said nothing at all, and
        flattening the two into one empty map deletes watch history over a
        transient failure, one show at a time, with nothing on the page to say so.
        Absence therefore means "I have nothing to tell you about this one" and
        the caller leaves what it already knew alone.

        A REFUSED CREDENTIAL IS NOT ONE SHOW FAILING and must raise rather than
        emptying the answer, for the reason SourceUnavailable exists: it is true of
        every request that token will make, so tolerating it per show composes a
        whole roster of refusals into a library nobody has watched.
        """
        ...

    async def fetch_watched_progress(self, settings: Settings,
                                     since_days: int | None = None) -> list[dict]:
        """Recently-active seasons, as candidates for "you seem to be watching
        this". A recency signal, not a completion record."""
        ...

    def watched_progress_from(self, events: list[dict]) -> list[dict]:
        """The same seasons, aggregated out of events the caller already has, as
        [{ids, season, watched, title, network}]. Pure. Here so a caller that
        needs both the seasons and the films from one window can sweep the history
        once, and so reading a source's event shape stays the source's job."""
        ...

    def movie_plays_from(self, events: list[dict]) -> list[dict]:
        """The film plays in those same events, as
        [{ids, title, year, watched_at}]. Pure, for the same reason."""
        ...


class UnlistedSeasons(StrEnum):
    """WHAT A SOURCE'S SILENCE ABOUT A SEASON OF A TITLE IT HOLDS MEANS.

    Three answers, because a library payload can be making any of three different
    statements with the same absent season, and they are not degrees of one
    another:

      SILENT   the payload says nothing about seasons it did not list. Reading
               anything into the gap would be inventing an answer.
      ZERO     the payload lists only the seasons with watches in them, so a
               season it did not list is one the viewer has seen NONE of. A
               season both services have seen none of is an agreement at zero,
               and recording nothing for it renders that agreement as a claim
               only one service made.
      WATCHED  the payload says the whole title is finished and itemizes nothing,
               so every season of it has been seen in full. Reading THAT as a
               zero is the same mistake with the sign flipped, and it is the
               worse of the two: it claims a service reported none of a title
               that service reports as complete.

    A SOURCE DECIDES THIS PER ENTRY AND NOTHING ELSE MAY. Which of the three is
    true is a fact about one payload — often about one list within it, since a
    service can itemize the titles in progress and count the finished ones — so
    it is stated where the payload is read and travels on the entry. SILENT is
    the default because a source that has not thought about the question must
    come out too cautious rather than confidently wrong.
    """
    SILENT = "silent"
    ZERO = "zero"
    WATCHED = "watched"


class LibraryEntry(NamedTuple):
    """What one source holds about ONE title in somebody's library.

    `ids` is every id space that source named the title in, and it is what makes
    a library read worth more than a progress read: the caller learns the
    source's own id for a title it had no id for, as a BY-PRODUCT of the match
    rather than as its precondition.

    `seasons` is {season: {episode: watched_at}} — the same shape a per-show
    progress record returns, so a caller folds either in through one path. An
    empty map is a real answer: the source holds the title and has seen none of
    it.

    `unlisted_seasons` SAYS WHAT THE SEASONS NOT IN THAT MAP MEAN — see
    UnlistedSeasons above, which is where the three answers and the reason for
    each are written.

    IT IS A PROPERTY OF THE PAYLOAD, WHICH IS WHY IT RIDES ON THE ENTRY AND NOT ON
    THE READ. The claim is only ever about a title this source HOLDS; a title
    absent from the read has no entry, so there is nothing to consult and no way
    for a caller to extend the claim to one.

    A SOURCE THAT CLAIMS `WATCHED` STATES A FACT AND NOT A COUNT, deliberately. A
    service that reports a title as finished without itemizing it has no episode
    numbers and no per-episode dates to hand over, and a provider that
    manufactured them would be inventing a viewing history to fit a shape. How
    many episodes that is, is the CALLER's question — it is the side holding the
    season's total, because it is the side that renders "watched out of total".
    """
    ids: dict[str, Any]
    seasons: dict[int, dict[int, str]]
    unlisted_seasons: UnlistedSeasons = UnlistedSeasons.SILENT


class LibraryRead(NamedTuple):
    """One pass over a person's library at one source.

    `entries` is keyed by the FLAT ItemKey — `str(resolve_key(...))` — because
    that is the identity the app files its own rows under, and keying on it here
    is the whole reason a caller never needs a per-source id in order to ask.
    Shows only: a film is a play on a day rather than a count out of a total, and
    it arrives through `events` like any other play.

    `events` are the plays inside that same read, in the shape
    `SyncPort.fetch_history` returns, so one pull answers both questions instead
    of two pulls answering one each.

    `complete` says whether this read covered the WHOLE library. A partial read
    is what makes skipping unchanged lists safe: a title missing from a complete
    read is a title the source does not hold, while a title missing from a
    partial one says nothing at all and its caller must leave what it already
    knew alone.
    """
    entries: dict[str, LibraryEntry]
    events: list[dict]
    complete: bool


@runtime_checkable  # see the note on SyncPort above
class LibraryPort(Protocol):
    """A source that can hand over a person's WHOLE library in one read, keyed by
    the shared title identity.

    SEPARATE FROM SyncPort, AND OPTIONAL, because it is a different question
    rather than a bigger version of the same one. `SyncPort.fetch_progress_details`
    asks "what has this person seen of the titles I can already name to you",
    which needs the source's own id for every title before it can be placed. This
    asks "what does this person's library say", and answers about titles the
    caller could not have named — which is the only way a roster built entirely
    from one service ever learns what a second service holds. A source with no
    endpoint that returns a whole library simply does not implement this and its
    caller keeps asking per title.

    THE IDENTITY RULE IS NOT RESTATED HERE OR IN ANY IMPLEMENTATION. `resolve_key`
    above is the one waterfall; an implementation runs its own payload's ids
    through it and nothing more. What an implementation DOES own is the payload
    shape — which field carries which id, and how its lists are spelled.
    """

    async def fetch_library(self, settings: Settings, *, start_at: str | None = None,
                            activities: dict | None = None,
                            since: dict | None = None) -> LibraryRead:
        """This person's library, and the plays inside it since `start_at`.

        `activities` is what this source's own `fetch_last_activities` returned
        for THIS pass, and `since` is what it returned at the end of the last
        successful one, or None. BOTH ARE OPAQUE TO THE CALLER — it hands back
        what the source gave it and reads nothing out of them. A source that
        publishes per-list change stamps uses the pair to read only the lists
        that moved and to skip one that has never been used at all, and says so
        by returning `complete=False`. A source that cannot tell ignores both,
        reads everything, and returns `complete=True`; that is why the default
        for both is None and why neither is required.
        """
        ...


class PlayCounts(NamedTuple):
    """How many plays a source has recorded against each title it holds for one
    person, keyed by THAT SOURCE'S OWN ID as a string.

    A COUNTER, NOT A WATCH RECORD, and the difference is the whole point. It says
    nothing about which episodes were seen — it cannot, and a source offering this
    typically has no cheap read that could. What it is for is telling a caller
    WHICH titles to ask about properly, so a re-baseline costs one call per title
    that actually moved rather than one per title ever tracked.

    IT HAS TO MOVE IN BOTH DIRECTIONS OR IT IS USELESS HERE. Measured against a
    live account by removing a season's plays and then re-marking them: the count
    fell and rose with the watched set, while the "last updated" stamp beside it
    moved only on the addition. A change detector keyed on a stamp like that
    silently misses every removal, which is the exact defect this exists to catch,
    reintroduced one layer down.

    The id is a STRING because it is a dict key that gets stored as JSON, where a
    number could not be one, and because the caller compares it against ids that
    arrive from storage in either form.

    `complete` says whether the sweep covered the whole listing. A partial sweep
    may say what it FOUND and may never be read for what is missing: a title
    absent from a page that was never fetched is indistinguishable from a title
    with no plays left, and only the second is a removal.
    """
    counts: dict[str, int]
    complete: bool


@runtime_checkable  # see the note on SyncPort above
class PlayCountPort(Protocol):
    """A source that can say, cheaply and for the whole library at once, which
    titles a person's watch record has MOVED for.

    A THIRD OPTIONAL PROTOCOL, beside SyncPort and LibraryPort, because it is a
    third question rather than a smaller version of either. LibraryPort answers
    "what does this person's library say", per episode, and a source that can do
    that needs nothing here — re-reading a list it holds re-states what it holds,
    so a removal inside one corrects itself. This answers only "what changed", and
    it exists for the source whose whole-library read carries no episodes at all:
    without it, the only way to find out what moved is to ask about every title,
    which is one call each and grows with everything ever tracked.

    A source that can answer neither is asked per title exactly as before.
    """

    async def fetch_play_counts(self, settings: Settings) -> PlayCounts:
        """Every title this person has plays against, and how many.

        Must be cheap relative to asking per title — that is the only reason to
        implement it. A sweep that could read nothing at all raises rather than
        answering with an empty map, for the reason every read here does: an empty
        map means "every title you had plays for has lost them", which is a
        removal of the entire library.
        """
        ...


@runtime_checkable  # see the note on SyncPort above
class Provider(Protocol):
    """What the registry needs in order to offer a source at all: who it is,
    what it can answer, and whether it is usable right now.

    KEPT NARROW ON PURPOSE. Detail lookups and search are each a different
    consumer with a different degradation story, and folding them in here would
    mean a source that only publishes a calendar could not be registered at all.
    They become their own protocols when a second source actually needs them —
    which is what `sync_port` below already is.

    THERE IS DELIBERATELY NO CALENDAR VERB HERE, and the gap is load-bearing.
    There used to be a `fetch_calendar(endpoint, settings, year, month)`, but
    nothing reached a user through it: the live path is a WINDOW fetch — a start
    date and a day count — driven by the calendar cache, because a month is not
    the unit Trakt is asked about and is not the unit the cache stores. A second
    source should be given the window shape the app actually uses, so an empty
    slot here is better than a method the first implementer would have to
    contradict.
    """
    source: Source
    label: str
    capabilities: Capabilities
    # The private reads, or None for a source that has none. Declared here rather
    # than left to a getattr at the call site because it is how the tracker finds
    # a source WITHOUT naming one, and a source claiming
    # `capabilities.private_user_data` while carrying no port would be lying in a
    # way nothing else can catch.
    sync_port: SyncPort | None

    def is_configured(self, settings: Settings) -> bool:
        """Whether this source has the credentials it needs to be asked
        anything. The registry uses it to pick a usable calendar source, so it
        must answer without making a network call."""
        ...
