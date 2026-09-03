"""The shape every calendar source must speak, and nothing about any one source.

This module is the seam. It declares WHAT a source produces (`Record`), WHAT a
template renders (`Item`), WHAT a source is able to answer (`Capabilities`), and
the ports the rest of the app calls a source through (`Provider` and friends). It
imports nothing from the rest of the app at runtime, so a provider implementation
can depend on it without anything depending back.

WHY A DATACLASS AND NOT A TypedDict OR A DICT. There is no type checker in this
project's CI, so a TypedDict would document the contract without enforcing it —
and the entire value of this seam is that a second source emits the SAME record
as the first. A dataclass raises at construction, inside that provider's own
tests, the moment a field is forgotten or invented. The templates already use
dot access, which reads identically either way.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import MISSING as MISSING_DEFAULT
from dataclasses import dataclass, field, fields
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol, runtime_checkable
from urllib.parse import quote
from zoneinfo import ZoneInfo

if TYPE_CHECKING:  # import-only-for-annotations: endpoints.py imports Media from here
    from ..config import Settings


# The image proxy every source's artwork is addressed through. Simkl's own image
# conventions name this proxy and `q=90` (https://api.simkl.org/conventions/
# images.md), which they document as matching their origin quality.
_IMAGE_PROXY = "https://wsrv.nl/"
# 440 IS TWICE THE WIDEST CARD (style.css's `--card-w: 220px`), so a retina
# screen gets its two device pixels per CSS pixel and nothing gets more than
# that. THE WIDTH IS NOT OPTIONAL, measured 2026-08-28 against real stored
# posters: unbounded, the proxy re-encodes Trakt's 600x900 original to WebP at
# q=90 and hands back 32.5 KB where the origin served 24.3 KB -- routing images
# through a proxy to be a better citizen would have made every card HEAVIER. At
# w=440 it is 24.6 KB, parity with the direct fetch at the size actually drawn.
#
# `we` IS "WITHOUT ENLARGEMENT" AND IS THE OTHER HALF OF THAT. wsrv.nl upscales
# by default: Simkl's origin is 340x500, and asking for 440 without this returned
# a 440x647 image of 77.7 KB against the origin's 43.3 KB -- half again the bytes
# for a picture that is genuinely blurrier. With it, a source smaller than the
# card is passed through at its own size.
_IMAGE_PARAMS = "q=90&w=440&we"


# WHAT THE SERVER ASKS FOR WHEN IT IS FETCHING A POSTER TO KEEP, rather than
# handing an address to a browser. Three differences from the card params above,
# each forced by what the bytes are for:
#   - AN EXACT SIZE. The picture is composited into a fixed grid by
#     app/calendar/share_card.py, whose tiles are 2:3; a variable-sized source
#     would have to be resized on this side, which is the work being moved.
#   - `fit=contain` WITH A BLACK CANVAS, which is a pad rather than a crop or a
#     stretch. A wrong-aspect poster from a fallback source must come out
#     letterboxed, never visibly distorted and never with its title cropped off.
#   - NO `we`. "Without enlargement" is right for a browser, which can scale a
#     small image down to the card; here the caller needs the exact canvas, so a
#     smaller origin is padded up to it.
# `output=jpg` because the compositor reads these back with Pillow and a
# predictable format is one less thing for it to negotiate.
POSTER_PARAMS = "w=500&h=750&fit=contain&cbg=000000&output=jpg&q=88"


def proxied_image(url: str | None, params: str | None = None) -> str | None:
    """A poster URL addressed through the shared image proxy.

    `params` overrides what is asked of the proxy, for a caller whose picture is
    not a card in a browser — see POSTER_PARAMS. The proxy HOST is not
    overridable, which is the part this function exists to keep in one place.

    ONE IMPLEMENTATION FOR EVERY SOURCE, which is the point of it living here
    rather than in either provider package. The rule is about how this app treats
    other people's image hosts, not about any one of them: Trakt asks that its
    images "be cached in your app or server and not loaded directly from our
    CDN", and Simkl publishes this exact proxy as their own recommended form. A
    per-provider copy would be the same policy written twice, free to drift, with
    neither copy able to say it was the rule.

    RETURNS THE INPUT UNCHANGED WHEN THERE IS NOTHING TO PROXY -- an empty or
    absent URL stays empty, so a title with no artwork does not acquire a proxy
    address that resolves to nothing. An already-proxied URL is left alone too,
    so a stored record read back and re-normalized cannot be wrapped twice.
    """
    if not url:
        return url
    text = str(url)
    if text.startswith(_IMAGE_PROXY):
        return text
    return f"{_IMAGE_PROXY}?url={quote(text, safe='')}&{params or _IMAGE_PARAMS}"


class SourceNotModified(Exception):
    """A source has confirmed that nothing it would return has changed.

    NOT A FAILURE, AND THE DISTINCTION IS THE WHOLE REASON IT IS ITS OWN TYPE.
    `SourceUnavailable` means "could not answer" and leaves a span partial;
    this means "answered, and the answer is the one you already stored". A
    caller keeps its rows, advances its schedule, and reports the source as
    having replied.

    It exists because a conditional GET's 304 carries no body. Under a design
    that stored the body beside the validator the distinction never surfaced --
    a 304 was served from the stored copy and looked like an ordinary answer.
    Storing only the validator is what makes "unchanged" something the caller
    has to be told rather than something the transport can hide.
    """


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
#
# A SLUG IS NAMESPACED PER SERVICE, unlike every shared id beside it, and for the
# same reason `trakt` and `simkl` are: it is a name you CALL one service with,
# and the two services do not agree on it. Trakt writes `the-traitors-2023` where
# Simkl writes `the-traitors`. A single `slug` key made the two collide in any
# merged id map — whichever service wrote last won — and BOTH links built from it
# were then wrong half the time, Trakt's as readily as Simkl's.
#
# `slug` IS STILL READ, never written. Rows predate the split and their value is
# usually but not provably Trakt's, so it stays a legitimate fallback for a
# reader that has no namespaced one yet (see store.ID_COLUMNS and the migration
# that backfills only what it can prove).
ID_KEYS = ("trakt", "slug", "trakt_slug", "simkl", "simkl_slug",
           "tvdb", "tmdb", "imdb", "mal")


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


# The fields on `Record` that resolution writes and storage never does. See
# `Record.field_sources` for what they hold and `Record.to_dict` for why they are
# the one thing excluded from a stored record by name.
PROVENANCE_FIELDS = ("field_sources", "alternatives", "source_links")


@dataclass
class Record:
    """One airing as a SOURCE describes it, said in a way that is true for
    everybody who might look at it.

    THIS IS WHAT THE CALENDAR CACHE STORES, and the reason it can be stored at
    all is that nothing on it depends on who is reading. `air_ts` is POSIX
    seconds — an absolute instant — and the four viewer-local spellings of that
    instant live on `Item` below, derived at read time by `render`. A record
    carrying "21:00" would be one viewer's 21:00 and would be wrong in the shared
    cache the moment a second timezone read it.

    `genres` ARE THE SOURCE'S RAW SLUGS, lowercase and hyphenated ("game-show"),
    NOT the title-cased display form. The per-viewer genre filter matches on the
    slug (app/calendar/filter.py says so), so a record holding "Game Show" would
    silently break every multi-word genre filter while leaving single-word ones
    working — which is about the hardest failure of this kind to notice. The
    title-casing happens in `render`, on the far side of the filter.

    NOT FROZEN: the calendar read path annotates the rendered items in place (day
    layout, per-viewer marks) and a frozen record would force a copy at each step
    for no safety anyone is currently relying on.

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
    title: str
    # The instant this airs, in POSIX seconds. The sort key, and the only time
    # fact a source has to supply.
    air_ts: float

    # WHETHER THAT INSTANT IS REALLY AN INSTANT. A movie's release is a calendar
    # FACT, not a moment: a film released on the 6th is released on the 6th
    # wherever you are. Trakt's `released` and every Simkl movie entry are plain
    # dates, and turning one into a UTC-midnight timestamp and then rendering it
    # in a viewer's timezone moves a UTC-8 viewer's release to the day before.
    # When this is set, `render` reads the date straight back out of `air_ts` in
    # UTC and does no conversion at all, so every viewer sees the date the source
    # published.
    date_only: bool = False

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
    # A THIRD PARTY'S SCORE, WHICH IS NOT A THIRD SPELLING OF `rating`. Trakt's
    # number and Simkl's are two audiences answering the same question, and the
    # card draws them side by side under each service's mark rather than
    # averaging them. IMDb's arrives THROUGH a source rather than from one this
    # app reads calendars from, so it competes with neither and is resolved as
    # its own field. Only Simkl reports it today; a record from a source that
    # does not carry it leaves this None, which is an absence like any other.
    imdb_rating: float | None = None
    genres: list[str] = field(default_factory=list)
    certification: str = ""
    overview: str = ""
    poster: str | None = None
    # Episode coordinates. Present on the show endpoints, absent on movies.
    episode_label: str | None = None   # "S02E05"
    episode_title: str = ""
    season: int | None = None
    episode_number: int | None = None

    # SIMKL'S OWN "IS THIS ACTUALLY A FILM" ANSWER, carried on the record so
    # app/calendar/filter.py's read-time prune can act on it — see
    # prune_disguised_films there for the rule and app/providers/simkl/
    # titles.py's `_extract` for where the value comes from. Empty for every
    # record no enrichment has looked up yet (including every non-Simkl
    # source, which never sets this at all) and for the small serial formats
    # ("ona", "ova", "tv", "special") that must NOT be pruned; only "movie"
    # means "this is a film masquerading on a series endpoint".
    anime_type: str = ""

    # WHERE AND HOW THIS FILM IS BEING RELEASED, as {country: [release type]} —
    # TMDB's numbering (1 premiere, 2 limited theatrical, 3 theatrical,
    # 4 digital, 5 physical, 6 TV), which is what the service publishes. Read by
    # app/calendar/filter.py's release rule and by nothing else; see
    # app/providers/simkl/titles.py's `_release_types_by_country` for where the
    # value comes from and why the dates that sit beside these types in the
    # payload are deliberately not kept.
    #
    # NOT `country` PLURAL, AND NOT A REPLACEMENT FOR IT. `country` is where a
    # title was MADE and is one value; this is a list, it is about distribution,
    # and the two disagree constantly — a film made in France released only in
    # Brazil answers FR to one and BR to the other, and both are true.
    #
    # ONLY ENRICHMENT EVER SETS IT, so it never reaches a stored window: a
    # calendar file carries no release schedule, and `to_dict` omits an empty
    # default factory. That is the same reason `enriched` exists — the filter
    # must be able to tell "no release blocks" from "nobody has looked yet".
    release_types_by_country: dict[str, list[int]] = field(default_factory=dict)

    # WHETHER genres/network/country/certification/runtime/status/overview ARE
    # REAL ANSWERS OR JUST THIS RECORD'S DEFAULTS. True for every source that
    # carries these fields on its calendar payload already (Trakt does, so it
    # never sets this). Simkl's calendar CDN files carry none of them — see
    # app/providers/simkl/calendar.py — so a Simkl record starts False and is
    # filled in by app/calendar/enrich.py's background drain; the per-viewer
    # genre/country/certification/network filter (app/calendar/filter.py) reads
    # this to tell "empty because there is nothing to say" apart from "empty
    # because we have not looked yet", and exempts the second rather than
    # judging it on values it cannot answer for.
    enriched: bool = True

    # WHO SUPPLIED WHAT, once several sources have described this airing.
    # {field: [source, ...]} for every field any of them filled in, and
    # {field: {source: value}} for the ones where they filled it in DIFFERENTLY.
    # Both are set by app/calendar/resolve.py and by nothing else, and both are
    # empty on a record only one source described — which is what keeps them free
    # for the overwhelming majority of instances, where they always will be.
    #
    # NOT STORED, AND THEY MUST NOT BECOME STORED. A record is what ONE source
    # said; these two are what several sources said WHEN COMPARED, which is an
    # answer that only exists after resolution has run, at read, over a window
    # filled without knowing who would read it. Writing them into a window would
    # be storing the comparison, and the whole reason resolution runs at read is
    # that a preference change must invalidate nothing. `to_dict` drops them for
    # the same reason it drops empty genres.
    field_sources: dict[str, list[str]] = field(default_factory=dict)
    alternatives: dict[str, dict[str, Any]] = field(default_factory=dict)

    # {source: detail_url} for every service that described this airing.
    #
    # WHY IT IS NOT `detail_url` PLURAL AND WHY IT IS NOT A PREFERENCE. Exactly
    # one service is the card's — see app/calendar/resolve.py's SOURCE_FIELD, and
    # `detail_url` is one of the three fields that travel with it, because a page
    # on one service is not an answer another service gave. This is the other
    # thing a merged card knows and had no way to say: that BOTH services have a
    # page for this title. It is a set of destinations, not a value anybody won,
    # so no preference orders it and resolution states no opinion about it — it is
    # collected, in declared source order, and offered.
    #
    # NOT STORED, for the same reason the two maps above are not: it exists only
    # after several records have been compared, which happens at read.
    source_links: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """The JSON-safe form the cache stores.

        A FIELD SITTING AT ITS DEFAULT IS OMITTED, which is what makes
        `from_dict`'s defaults load-bearing rather than decorative: the common
        record has no certification, no language and no episode title, so the
        round trip is exercised on every single window rather than only when a
        new field is added. The enums are already strings (StrEnum), so they
        serialize as themselves.

        A FIELD WITH A DEFAULT FACTORY IS OMITTED WHEN IT IS EMPTY, which is the
        same rule said for the fields whose default cannot be compared against.
        An empty list is what `from_dict` builds when the key is absent, so
        writing one out says nothing and costs bytes in every stored window.

        THE PROVENANCE MAPS ARE OMITTED EVEN WHEN THEY ARE FULL, which is the one
        exclusion by name here and is not an optimization. They say what several
        sources said WHEN COMPARED — an answer that only exists after resolution
        has run, at read, for one account. A window is filled without knowing who
        will read it, so a comparison written into one would be one account's
        answer in a row served to everybody, and the next preference change would
        have to invalidate it. That is the exact coupling resolution runs at read
        to avoid.
        """
        out: dict[str, Any] = {}
        for f in fields(self):
            if f.name in PROVENANCE_FIELDS:
                continue
            value = getattr(self, f.name)
            if f.default is not MISSING_DEFAULT and value == f.default:
                continue
            if f.default_factory is not MISSING_DEFAULT and not value:
                continue
            out[f.name] = value
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Record":
        """A stored record, with EXPLICIT DEFAULTS FOR EVERY OPTIONAL FIELD.

        Caching normalized data means a normalizer change does not take effect
        until the window's TTL expires, and across a deploy there will be rows in
        the old shape sitting beside new ones. The version envelope
        (app/calendar/cache.py) handles a change big enough to invalidate; a
        field merely ADDED to this class has to be tolerated as missing, which is
        what this does. A row written before a field existed reads as a record
        that simply does not carry it.

        Raises for a row missing one of the fields that has no default — that row
        is not a record at all, and the caller treats the whole window as a miss.
        """
        kwargs: dict[str, Any] = {}
        for f in fields(cls):
            if f.name in data:
                kwargs[f.name] = data[f.name]
            elif f.default is MISSING_DEFAULT and f.default_factory is MISSING_DEFAULT:
                raise ValueError(f"A stored record is missing {f.name!r}.")
        kwargs["source"] = Source(kwargs["source"])
        kwargs["media"] = Media(kwargs["media"])
        kwargs["air_ts"] = float(kwargs["air_ts"])
        return cls(**kwargs)


@dataclass
class Item(Record):
    """A Record as ONE VIEWER sees it: the same facts, plus the four spellings of
    its air time in that viewer's timezone.

    A SUBCLASS RATHER THAN A SEPARATE TYPE, so every template, filter and share
    page that reads `item.title` or `item.genres` today reads it unchanged, and
    anything that only needs the source's facts can take a Record and be handed
    an Item. The four fields below are the whole difference, and `render` is the
    only thing that should ever set them.
    """
    air_date: str = ""      # YYYY-MM-DD, local
    air_display: str = ""   # "03 Jul 2026"
    air_time: str = ""      # "21:00"
    day_of_week: str = ""   # "Friday"
    # THE GENRES AS THEY ARE MATCHED, beside `genres` as they are SHOWN. The
    # display form is lossy in the one direction that matters: "Game Show" cannot
    # be turned back into "game-show" by any rule this app should rely on, and a
    # filter spec is written in slugs. Anything that offers to FILTER on a genre
    # a card is drawing needs the slug the card was drawn from, so it is carried
    # rather than reconstructed — see app/calendar/filter.py's own warning that
    # matching the display form breaks every multi-word genre while leaving
    # single-word ones working.
    genre_slugs: list[str] = field(default_factory=list)

    @property
    def mark_key(self) -> str:
        """This card's identity, said without naming who described it.

        WHY IT IS NOT `id`. `Record.id` is the SOURCE's own id, and a card is a
        merged group — so which id it carries depends on whose description won,
        which depends on the viewer's own preference. Anything keyed on that
        moves when the preference moves. Observed: a viewer with `the-game`
        marked not-watching saw the show reappear on reordering their sources,
        because the card's id became Trakt's `the-game-2025` and the mark was
        filed under Simkl's spelling. One title, two ids, and a per-viewer
        setting deciding which one a per-viewer mark had to match.

        THE SAME WATERFALL THE GROUPING USES (`resolve_key`), so a card's
        identity and the grouping's idea of "the same title" cannot disagree —
        they are one answer. A title the waterfall cannot key falls back to its
        source and id, which is safe precisely because such a title never merges
        with anything: there is only one description of it to prefer.
        """
        identity = resolve_key(self.media, self.ids)
        return str(identity) if identity is not None else f"{self.source}:{self.id}"

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def epoch_moment(air_ts) -> datetime:
    """A record's air time as a UTC instant, for ANY value it can hold.

    NOT `datetime.fromtimestamp`, AND THAT IS THE WHOLE POINT. On Windows that
    function hands the value to the platform C library, which REFUSES anything
    before 1970 with OSError [Errno 22] — so a title that first aired in 1969
    crashed the request rather than rendering. It reached production through the
    calendar search: a catalogue lookup answers for whatever a service knows,
    and services know about television older than the epoch.

    ARITHMETIC ON A FIXED EPOCH IS PURE PYTHON and has no such range. It agrees
    with `fromtimestamp` exactly wherever `fromtimestamp` works, so this is a
    widening rather than a change: every caller converting a record's air time
    should use it, because "which years can this app display" must not depend on
    which operating system it is running on.
    """
    return _EPOCH + timedelta(seconds=float(air_ts))



def render(record: Record, tz: ZoneInfo) -> Item:
    """The Item one viewer sees for `record`.

    THE ONE PLACE A STORED RECORD BECOMES A RENDERED ONE. Two things happen here
    and nowhere else, because both are per-viewer or per-display and neither may
    be baked into the shared cache:

      - the four air-time fields are derived from `air_ts`, in `tz` — unless
        `date_only` is set, in which case the date is read back in UTC and no
        conversion happens at all (see Record.date_only for why a release date is
        not an instant);
      - the genre slugs become their display form ("game-show" -> "Game Show").
        This is after every filter has run, which is the entire reason it is here
        rather than in a normalizer.
      - the poster is addressed through the image proxy.

    WHY THE PROXY IS A RENDER STEP AND NOT A NORMALIZER ONE, which is a
    distinction worth stating because getting it wrong is invisible. What the
    services ask is that their CDN not be hotlinked BY A BROWSER; Trakt's wording
    is that images be "cached in your app or server", and a server-side download
    is that, not a breach of it. `Record.poster` is read by both — the card
    hotlinks it, and app/media/artwork.py files it in the poster registry that
    app/media/posters.py later DOWNLOADS from. Proxying at the normalizer sends
    this app's own download through somebody else's cache for no reason and
    stores a proxy address where the origin belongs, so the registry can no
    longer say where a picture came from. Proxying here reaches the browser and
    nothing else.
    """
    moment = epoch_moment(record.air_ts)
    if not record.date_only:
        moment = moment.astimezone(tz)
    values = {f.name: getattr(record, f.name) for f in fields(Record)}
    slugs = [str(g) for g in record.genres]
    values["genres"] = [g.replace("-", " ").title() for g in slugs]
    # BOTH SIDES OR NEITHER. `alternatives["poster"]` holds each service's own
    # address, and the card decides which service is on screen by asking which of
    # those the rendered poster EQUALS (see _card.html's `won_by`) — so proxying
    # one and not the other makes every card claim it does not know whose picture
    # it is showing. The source-swap control has the same problem one step later:
    # it swaps in a value straight out of this map, which would hotlink the very
    # CDN the proxy exists to keep this app off.
    values["poster"] = proxied_image(record.poster)
    if record.alternatives.get("poster"):
        values["alternatives"] = {
            **record.alternatives,
            "poster": {name: proxied_image(url)
                       for name, url in record.alternatives["poster"].items()},
        }
    return Item(
        **values,
        genre_slugs=slugs,
        air_date=moment.strftime("%Y-%m-%d"),
        air_display=moment.strftime("%d %b %Y"),
        air_time=moment.strftime("%H:%M"),
        day_of_week=moment.strftime("%A"),
    )


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
class CalendarPort(Protocol):
    """A source that can say what airs in a stretch of days.

    A FOURTH PROTOCOL, beside SyncPort, LibraryPort and PlayCountPort, and
    deliberately not folded into any of them: this is the only one that answers a
    question about the WORLD rather than about one person, which is why it needs
    no token, why its answers are cached globally, and why a source that has
    nothing else to offer can still be registered for it.

    THE UNIT IS A WINDOW, NOT A MONTH. A start date and a day count is what the
    calendar cache stores and therefore what it asks for; a month is the thing a
    viewer looks at and is assembled from several windows, each shared between
    every viewer of every month that overlaps it.
    """

    def calendar_configured(self, settings: Settings) -> bool:
        """Whether this instance can read THIS SOURCE's calendar at all.

        THE SAME SHAPE `DetailPort.catalogue_configured` DRAWS, and on the PORT
        for a reason that is sharper here than anywhere else in this file: what a
        calendar costs to read differs per source more than any other question
        asked of a provider. Trakt's calendar authenticates with the instance's
        client id; another source's is a set of public files on a CDN that takes
        no credential of any kind. A predicate on the PROVIDER — `is_configured`
        or `catalogue_is_configured` — answers for the source as a whole and
        would therefore gate a calendar on a credential that calendar never
        sends, which is how an instance with a working public calendar came to be
        told it had no calendar source at all.

        NOT `is_configured`, WHICH IS THE PRIVATE QUESTION. A calendar read never
        uses a viewer's token — `calendar_sources` says so at length and then
        `for_calendar_sources` used to put that filter straight back — so the
        answer here is about the INSTANCE and never about who is looking.

        Must answer without a network call: it gates the call.
        """
        ...

    async def fetch_window(self, endpoint, settings: Settings,
                           start: date, days: int,
                           revalidate: bool = True) -> list["Record"]:
        """What this source says airs in [start, start + days), as Records.

        `revalidate=False` FORBIDS ANSWERING "unchanged". A source that caches a
        conditional-GET validator may normally raise SourceNotModified instead of
        re-deriving records — but that is only sound when the CALLER STILL HOLDS
        the rows it derived last time. A validator is per FILE while rows are per
        (endpoint, span), and several endpoints can read one file: the first to
        fill it records the validator, and every sibling then gets a 304 for a
        span it has nothing stored for. The caller knows which of those it is;
        this is how it says so. A source with no validators ignores it.

        NORMALIZING IS THE SOURCE'S JOB AND IS DECLARED HERE SO IT CAN ONLY BE
        DONE ONCE. The alternative — handing back a payload for somebody else to
        interpret — means the interpreting side learns every source's field
        layout, which is exactly what stops a third source being an addition
        rather than an edit.

        `endpoint` is one of app/endpoints.py's source-neutral calendar keys; a
        source that cannot answer it says so through `Capabilities.endpoints` and
        is never asked. Raises SourceUnavailable when the source could not be
        read at all — the caller degrades one source, or one window, rather than
        failing a month.

        A SOURCE MAY RETURN MORE THAN IT WAS ASKED FOR and the caller trims:
        Trakt treats `days` as a floor rather than a ceiling (measured — a 7-day
        window came back carrying entries two months past its end), so the bound
        is a request, not a promise.
        """
        ...


class SeasonsAnswer(NamedTuple):
    """The season picker's whole answer for one title, from one per-title lookup.

    THREE FACTS, ONE VERB, BECAUSE ONE LOOKUP ANSWERS ALL THREE ON THE SOURCE
    THAT NEEDS THEM. A season-title source (Simkl, for anime) states its own
    season and every shared id it knows on the SAME per-title record its season
    list already has to be read from — so a search hit that source left bare of
    a shared id is resolved at the one moment resolving it is free, and a hit
    that already names its season skips a picker with nothing left to ask. A
    caller that wanted this as two verbs would pay for the same lookup twice.

    `seasons` is [{season, episode_count}] — a show's seasons this source has
    populated with episodes, for a picker to offer. Empty for a source or a
    media kind that has none to offer (a movie, or a lookup that found
    nothing).

    `named_season` is the season THIS hit's own per-title record already names,
    or None — either because the source never says a hit is one season of a
    larger show (Trakt, always), or because a season-title's own mapping is
    missing or names more than one season of the show it belongs to. AN
    AMBIGUOUS MAPPING IS NOT GUESSED AT: it comes back as None, exactly like no
    mapping at all, and a caller falls back to `seasons` and lets somebody
    choose — see the Simkl implementation for what "missing or ambiguous"
    means against its actual payload.

    `ids` IS collect_ids()-FILTERED, AND IT IS ONLY WHAT THIS LOOKUP SURFACED —
    not a caller's own id map merged in, because this function does not have
    one to merge with. Empty for a source whose search hit already carries
    every shared id it will ever have (Trakt, measured), which makes unioning
    it into whatever a caller already knew a no-op rather than a special case.

    `network` IS THE SAME BARGAIN AS `ids`, FOR THE SAME REASON: what this ONE
    lookup surfaced, "" where the source did not say or where its search hit
    already carried it (Trakt, measured). It is on this answer rather than
    fetched separately because a source whose search hit has no network
    (Simkl, measured) does carry one on the per-title record the season list
    is read from anyway — and a caller with only that source has no other
    source to fill the gap from, which is how a show added by hand reached a
    roster with no network at all.
    """
    seasons: list[dict]
    named_season: int | None
    ids: dict[str, Any]
    network: str


@runtime_checkable  # see the note on SyncPort above
class DetailPort(Protocol):
    """A source that can describe ONE TITLE as fully as it knows how — what the
    detail modal draws.

    A FIFTH PROTOCOL, and it exists now for the reason `Provider`'s own docstring
    below gives for keeping itself narrow: a detail lookup is a different
    consumer with a different degradation story from a calendar window, so it
    becomes its own protocol the moment a second source needs one. That moment
    has arrived — a title only one service listed has to be describable by that
    service or its card opens on nothing.

    LIKE CalendarPort, THIS ASKS ABOUT THE WORLD RATHER THAN ABOUT ONE PERSON.
    Every answer is identical for every viewer, which is why it caches globally
    and why a viewer who has linked nothing still gets one.

    THE ANSWER'S KEY SET IS THE SAME WHOEVER GIVES IT, and that is the whole
    reason this is a protocol rather than two functions the route branches
    between. One client-side renderer draws the modal, so a key present on only
    one source's answer would read as a template bug rather than as a lookup that
    source cannot make. A source with nothing to say for a key says it with an
    empty value — which is exactly what `cache_only` already produces on a source
    that CAN answer, so the renderer has always had to tolerate it.
    """

    def catalogue_configured(self, settings: Settings) -> bool:
        """Whether this instance can make this source's PUBLIC per-title reads.

        DELIBERATELY NOT `Provider.is_configured`, and the pair is the same split
        `Settings.trakt_catalogue_configured` draws beside `trakt_configured`:
        that one asks whether somebody's PRIVATE data can be read, which needs a
        token, and asking it in front of a public catalogue lookup makes an
        instance-wide, globally cached fact hinge on one account's credential.
        Two sources make the gap bigger rather than smaller — Simkl's catalogue
        takes a client id and no token at all, so an instance that has never
        issued a Simkl token can still describe every title Simkl listed.

        Must answer without a network call: it gates the call.
        """
        ...

    async def fetch_details(self, settings: Settings, media, source_id,
                            season: int | None, *, cache_only: bool = False) -> dict:
        """One title, in THIS SOURCE's own id space, as the modal's field set.

        `source_id` is the id this source knows the title by — the value under
        this source's own name in a group's `ids` map — and never another
        service's, because a source cannot look a title up by an id it does not
        issue.

        `cache_only=True` serves whatever is already cached and makes no outbound
        call, which is what the public share pages use so a visitor's click can
        never spend the owner's rate limit. Fields with nothing cached behind them
        come back empty and the modal renders around them.
        """
        ...

    async def fetch_season_summary(self, settings: Settings, source_id,
                                   season: int, media: Media) -> dict:
        """ONE season reduced to the shape `app/providers/season.py` defines —
        total, cadence, premiere, finale, the started/finished flags and
        `air_dates` — in THIS SOURCE's own id space.

        A PORT BECAUSE THE ANSWER WAS ONLY REACHABLE FROM ONE SOURCE. Both
        packages have had this function with these exact keys for as long as the
        tracker has had tiles, and every caller reached the Trakt one by name.
        The calendar's own season line therefore appeared on a card only when the
        title happened to carry a Trakt id: a Simkl-only card could never show
        it, not because Simkl cannot answer but because nothing asked. Naming it
        here is what makes "which source answers for this title" the same
        question for a season summary as it already is for a description.

        `source_id` is the id this source knows the title by, never another
        service's, for the reason `fetch_details` gives above.

        Raises this source's own `SourceUnavailable` subclass on a genuine
        failure, and answers an EMPTY season (`season.empty_season`) for one that
        does not exist. Those are different answers and callers act on the
        difference — see the Trakt implementation for what conflating them cost.
        """
        ...

    async def fetch_seasons(self, settings: Settings, source_id, media: Media) -> SeasonsAnswer:
        """The season picker's answer for one title, in THIS SOURCE's own id
        space — see `SeasonsAnswer` for why the season list, a self-named
        season and newly-surfaced ids all come back from one call.

        `source_id` is this source's own id for the title, the same value
        `fetch_details` takes it as and for the same reason: a source cannot
        look a title up by an id it does not issue.

        Raises this source's own `SourceUnavailable` subclass on a genuine
        failure — the same distinction every other port in this file draws
        between "asked and found nothing" and "could not be asked at all".
        """
        ...


class SearchHit(NamedTuple):
    """One title a catalogue search answered with — not a Record, because a
    search result is not an airing: it has no date, and the tracker's add
    flow is the only reader, so this carries what THAT flow needs and nothing
    a service happened to send.

    A NamedTuple rather than a dict for the same reason every other port's
    payload is declared (see this module's own docstring): two packages
    implement SearchPort and `tests/providers/test_protocol_conformance.py`
    can only hold them to one shape if the shape is stated somewhere.

    `source` AND `source_id` TRAVEL TOGETHER because a pick has to call back
    to the service that answered — the season list and the per-title
    resolution both come from a per-title lookup keyed on THAT source's own
    id, and a hit carries no id space every source shares to be found by
    instead. Carried rather than re-derived from which of `ids` happens to be
    present, which would be a guess dressed as a derivation.

    `season` IS None FOR EVERY HIT AT SEARCH TIME ON EVERY SOURCE MEASURED SO
    FAR — Trakt's search never returns a season, and Simkl's search hit
    carries no season either, even though Simkl's PER-TITLE record does (a
    season-title's season number lives one lookup deeper). It is on this
    shape anyway because a hit that IS a season, once a caller resolves one,
    needs somewhere honest to put that fact, and `None` still means exactly
    what it means everywhere else in this file: "this is the whole show."

    `network`, `runtime` AND `overview` ARE EMPTY WHEN A SOURCE DOES NOT SAY,
    not omitted — Simkl's search hit carries none of the three, measured live,
    while Trakt's carries all three. A caller merging several sources' answers
    for the same title can fill a gap from whichever source did say, the same
    rule `Record.enriched` exists for on the calendar side.

    `ids` IS collect_ids()-FILTERED, AND CARRIES THE WHOLE MAP. An id dropped
    here is one a later match against another service's search hit could not
    use — the same reasoning trakt/detail.py's `ids_map` states for its own
    per-title lookups, and it matters more here: a caller merging two sources'
    hits for one title dedupes and unions on exactly these ids.
    """
    source: Source
    source_id: str
    media: Media
    ids: dict
    title: str
    year: int | None
    season: int | None
    network: str
    runtime: int | None
    overview: str


@runtime_checkable  # see the note on SyncPort above
class SearchPort(Protocol):
    """A source that can answer a free-text catalogue search — what the
    tracker's manual add flow asks before anything is picked.

    A SIXTH PROTOCOL, AND A NEW KIND OF QUESTION FROM DetailPort BESIDE IT.
    DetailPort describes ONE title a caller can already name; this is asked
    with nothing but a string and answers with however many titles might
    match it. Folding search into DetailPort would mean a source that can
    describe a title by id but cannot be searched — or the reverse — could
    not be registered honestly for the half it actually has.

    LIKE DetailPort AND CalendarPort, THIS ASKS ABOUT THE WORLD RATHER THAN
    ABOUT ONE PERSON: a catalogue search authenticates with the instance's own
    credential on every source registered so far, never with a viewer's token,
    which is why `Provider.catalogue_is_configured` — the predicate that gates
    this port, not `is_configured` — asks the same public question
    `DetailPort.catalogue_configured` does for per-title lookups.
    """

    async def search_titles(self, settings: Settings, media: Media, query: str) -> list[SearchHit]:
        """Every title this source's catalogue matches `query` on, for
        `media`. An empty query answers [] without a call — there is nothing
        to ask a service for.

        RAISES THE SOURCE'S OWN SourceUnavailable SUBCLASS RATHER THAN
        RETURNING [] on a failure that is not "no matches" — a caller reading
        several sources needs to tell "this source found nothing" apart from
        "this source could not be asked", the same distinction every other
        port in this file draws, and collapsing them here would report a
        search that could not be made as a search that succeeded with an
        empty answer.
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
    which is what `sync_port` below already is, and what `detail_port` became
    once a title only one service listed had to be describable by that service.

    THE CALENDAR VERB IS A PORT RATHER THAN A METHOD, and the shape it has is
    the shape the app actually uses. There used to be a
    `fetch_calendar(endpoint, settings, year, month)` here that nothing reached a
    user through, because the live path is a WINDOW fetch — a start date and a
    day count — driven by the calendar cache: a month is not the unit a source is
    asked about and is not the unit the cache stores. `calendar_port` below is
    that verb, declared once, in the shape it is really called in.
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
    # What airs, or None for a source that publishes no calendar. Declared beside
    # `sync_port` for the same reason: the calendar cache asks the registry which
    # sources can answer and never names one, and a source declaring
    # `capabilities.endpoints` while carrying no port would be claiming a calendar
    # it cannot produce.
    calendar_port: CalendarPort | None
    # How this source describes ONE title, or None for a source that cannot.
    # Declared here for the same reason as the two ports above: the detail modal
    # asks the registry which source can describe the title in front of it and
    # never names a service, so a card from a source nobody added a port for
    # opens on an honest refusal rather than on an AttributeError.
    detail_port: DetailPort | None
    # How this source answers a catalogue search, or None for a source that
    # cannot be searched. Declared for the same reason as the three ports
    # above: `for_catalogue_search` asks the registry which sources can answer
    # a search and never names one, so a source claiming search while carrying
    # no port would be lying in the one way the registry cannot catch.
    search_port: SearchPort | None

    def is_configured(self, settings: Settings) -> bool:
        """Whether this source has the credentials it needs to be asked
        anything. The registry uses it to pick a usable calendar source, so it
        must answer without making a network call."""
        ...

    def catalogue_is_configured(self, settings: Settings) -> bool:
        """Whether this instance can make this source's PUBLIC catalogue
        reads — the predicate `for_catalogue_search` gates on, and
        deliberately not `is_configured` above.

        THE SAME SPLIT `DetailPort.catalogue_configured` DRAWS, ASKED AT THE
        PROVIDER LEVEL rather than the detail port's, because
        `for_catalogue_search` picks which sources to ask before any of them
        has been asked to describe a title — it needs the answer off the
        Provider itself, the same object `is_configured` already lives on,
        not off a port that may be None for a source with no detail
        implementation. Two predicates stating the same instance-wide fact in
        two places would be one more pair to keep in step for no reason
        `DetailPort.catalogue_configured` does not already serve its own
        caller.
        """
        ...
