"""The chip vocabularies the filters panel offers, and the form-field encoding
that carries a viewer's answers back.

WHY BOTH LIVE IN ONE MODULE. The panel renders a chip per token and the route
reads the answers back; if the vocabulary sat in the template and the parsing
sat in the route, the two would be one typo apart from a chip nobody can set.
Here the template renders from `chips_for` and the route reads with
`specs_from_form`, and both spell the field name through `field_name` — so the
name format is stated once and neither side can drift from it.

WHAT A CHIP FIELD LOOKS LIKE ON THE WIRE:

    chip:tv_genres:drama  =  ""  |  "include"  |  "exclude"

One field per token, one string per field, and nothing nested. That shape is not
an accident: mutating requests here are `application/json` only (app/authz.py's
request_shape_guard), so the panel posts JSON, and the htmx json-encoding
extensions this app is heading towards serialize a form as flat string
name/value pairs with no dependable array convention. A payload that is already
flat strings needs no server change the day the submit handler is replaced by
`hx-post`.

THE SPEC FORMAT ITSELF IS NOT RESTATED HERE. What a leading '-' means, which
dimensions fold case, that networks are a list rather than a comma string — all
of that is app/calendar/filter.py's, and this module assembles specs by calling
its `merge_token` one token at a time rather than by joining strings itself. So
a chip pressed in this panel and a badge pressed on a card write through the
same code, and there is no second implementation of the format to keep in step.

A TOKEN THE VOCABULARY DOES NOT NAME IS STILL A TOKEN. Free text typed into a
field, and anything a card badge added, arrives as a chip field with exactly the
same name shape — see `chips_for`, which returns the vocabulary's chips followed
by whatever else the stored spec holds. That is what lets the panel show every
answer a viewer has given without the vocabulary having to be exhaustive.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from .. import route_params
from ..providers.base import SHOW_TYPES, Media
from . import filter as calendar_filter

# The pref field each dimension writes into. These are `user_prefs` column names
# (app/auth/prefs.py) and they are what a chip field's middle segment carries, so
# a form field names its destination directly and the route needs no translation
# table between the two.
TV_GENRES = "tv_genres"
MOVIE_GENRES = "movie_genres"
TV_COUNTRIES = "tv_countries"
MOVIE_COUNTRIES = "movie_countries"
SHOW_CERTIFICATIONS = "show_certifications"
MOVIE_CERTIFICATIONS = "movie_certifications"
NETWORK_FILTER = "network_filter"
MOVIE_RELEASE_COUNTRIES = "movie_release_countries"
MOVIE_RELEASE_TYPES = "movie_release_types"
TV_LANGUAGES = "tv_languages"
MOVIE_LANGUAGES = "movie_languages"
# Stored in `tv_keywords_json` / `movie_keywords_json`, the way `network_filter`
# is stored in `network_filter_json`: app/auth/prefs.py does the translation.
TV_KEYWORDS = "tv_keywords"
MOVIE_KEYWORDS = "movie_keywords"
TV_SHOW_TYPES = "tv_show_types"
TV_MISSING = "tv_missing"
MOVIE_MISSING = "movie_missing"
# The three dimensions that are a SWITCH rather than a set of tokens.
TV_SINGLE_SERVICE = "tv_single_service"
MOVIE_SINGLE_SERVICE = "movie_single_service"
TV_SPARSE_NO_NETWORK = "tv_sparse_no_network"

# The dimensions stored as a LIST rather than a comma string, and filter.py's
# `merge_token` needs telling which it is being handed. Networks, because the
# textarea that first wrote them split on commas before storage; keywords,
# because a TMDB keyword's own name can hold a comma. Stated once here so no
# caller has to remember it.
LIST_FIELDS = frozenset({NETWORK_FILTER, TV_KEYWORDS, MOVIE_KEYWORDS})

# WHICH LIST KEEPS ITS CASE. Every comma-string dimension folds, and filter.py's
# own parsers are where that is decided, beside the matching it has to agree
# with; the two LIST dimensions differ, and this names which parser reads which.
# Networks keep it ('TVN' and 'tvN' are two broadcasters — parse_network_spec);
# keywords fold (TMDB spells every keyword in lower case — parse_keyword_spec).
_LIST_PARSERS = {
    NETWORK_FILTER: calendar_filter.parse_network_spec,
    TV_KEYWORDS: calendar_filter.parse_keyword_spec,
    MOVIE_KEYWORDS: calendar_filter.parse_keyword_spec,
}
CASED_FIELDS = frozenset({NETWORK_FILTER})

# The dimensions that are one switch each, posted as `toggle:<column>`.
TOGGLE_FIELDS = frozenset({TV_SINGLE_SERVICE, MOVIE_SINGLE_SERVICE, TV_SPARSE_NO_NETWORK})

# The dimensions drawn as TICK BOXES rather than three-way chips: a missing-data
# rule is on or off, never "only titles missing a country". Each box posts its
# chip field as `include` or nothing, so the payload, the save and the stored
# spec are exactly the chips' — the ticked rules, comma-separated.
TICK_FIELDS = frozenset({TV_MISSING, MOVIE_MISSING})


def parse(field: str, spec) -> tuple[set[str], set[str]]:
    """`spec` as (includes, excludes), read by the parser filter.py matches this
    dimension with — so a chip drawn here and a title judged there fold, or keep,
    case the same way."""
    parser = _LIST_PARSERS.get(field)
    if parser is not None:
        return parser(spec or ())
    return calendar_filter.parse_spec(str(spec or ""))

# The three answers a chip can carry, and the empty one is a real answer: it is
# what "clear this" posts, and a field left out of the payload entirely would be
# indistinguishable from a chip the viewer never touched.
INCLUDE = "include"
EXCLUDE = "exclude"
IGNORED = ""
MODES = frozenset({INCLUDE, EXCLUDE, IGNORED})


@dataclass(frozen=True)
class Chip:
    """One offered token. `token` is what the filter matches; `label` is what the
    chip says.

    THE TWO DIFFER MORE OFTEN THAN THEY LOOK LIKE THEY WOULD. A genre matches as
    `science-fiction` and reads "Sci-Fi"; a release type matches as `4` — TMDB's
    numbering, which is what the service publishes — and reads "Digital". Every
    place that draws one of these already had to make that distinction, and the
    card badge macro (app/templates/_filter_badge.html) makes exactly the same
    one for exactly the same reason.
    """
    token: str
    label: str


def _chips(*pairs: tuple[str, str]) -> tuple[Chip, ...]:
    return tuple(Chip(token, label) for token, label in pairs)


# ---- The vocabularies ----
#
# CURATED AND STATIC, not derived from what a viewer's cached months happen to
# contain. A set that shifted month to month would move a chip out from under
# somebody mid-sentence, and a filter already set could lose the chip that set
# it. Anything the list does not name is still reachable by typing it, so the
# list only has to be USEFUL, not complete.
#
# THE TWO GENRE LISTS DIFFER ON PURPOSE. Trakt's genre vocabulary is shared
# across media, but half of it never appears on a film — nobody filters movies by
# "talk show" — and a chip that can never match is a chip that costs a reader
# time. Each list is the shared vocabulary minus what that medium never carries.

_GENRE_LABELS: dict[str, str] = {
    "action": "Action", "adventure": "Adventure", "animation": "Animation",
    "anime": "Anime", "biography": "Biography", "comedy": "Comedy",
    "crime": "Crime", "documentary": "Documentary", "drama": "Drama",
    "family": "Family", "fantasy": "Fantasy", "game-show": "Game Show",
    "history": "History", "holiday": "Holiday", "home-and-garden": "Home & Garden",
    "horror": "Horror", "mini-series": "Mini-Series", "musical": "Musical",
    "mystery": "Mystery", "news": "News", "reality": "Reality",
    "romance": "Romance", "science-fiction": "Sci-Fi", "short": "Short",
    "soap": "Soap", "special-interest": "Special Interest",
    "superhero": "Superhero", "suspense": "Suspense", "talk-show": "Talk Show",
    "thriller": "Thriller", "war": "War", "western": "Western",
}

_TV_GENRE_TOKENS = (
    "action", "adventure", "animation", "anime", "comedy", "crime", "documentary",
    "drama", "family", "fantasy", "game-show", "history", "home-and-garden",
    "horror", "mini-series", "mystery", "news", "reality", "romance",
    "science-fiction", "soap", "special-interest", "superhero", "suspense",
    "talk-show", "thriller", "war", "western",
)

_MOVIE_GENRE_TOKENS = (
    "action", "adventure", "animation", "anime", "biography", "comedy", "crime",
    "documentary", "drama", "family", "fantasy", "history", "holiday", "horror",
    "musical", "mystery", "romance", "science-fiction", "short", "superhero",
    "suspense", "thriller", "war", "western",
)

# THE COUNTRY LIST IS SHORT ON PURPOSE. There are ~250 ISO codes and a grid of
# them is a worse control than a text field. These are the ones this app's own
# calendars actually produce in volume; everything else is one keystroke away.
_COUNTRY_TOKENS = (
    ("us", "US"), ("gb", "UK"), ("ca", "Canada"), ("au", "Australia"),
    ("ie", "Ireland"), ("fr", "France"), ("de", "Germany"), ("es", "Spain"),
    ("it", "Italy"), ("se", "Sweden"), ("dk", "Denmark"), ("no", "Norway"),
    ("nl", "Netherlands"), ("jp", "Japan"), ("kr", "South Korea"),
    ("cn", "China"), ("in", "India"), ("br", "Brazil"), ("mx", "Mexico"),
)

# A SHORTER LIST FOR RELEASE MARKETS, and the difference is the question. The
# countries above ask where a title was MADE, which is a long tail — plenty of
# calendars carry Danish and Korean production. A release market is where a film
# is being SHOWN, and somebody narrowing that is almost always naming the one or
# two markets they live in.
_RELEASE_MARKET_TOKENS = (
    ("us", "US"), ("gb", "UK"), ("ca", "Canada"), ("au", "Australia"),
    ("ie", "Ireland"), ("fr", "France"), ("de", "Germany"), ("es", "Spain"),
    ("it", "Italy"), ("jp", "Japan"), ("kr", "South Korea"), ("in", "India"),
    ("br", "Brazil"), ("mx", "Mexico"),
)

# TMDB's release-type numbering, which Simkl reproduces and which app/db.py's
# migration 26 explains at length: the numbers are stored rather than names so
# the stored value is the vocabulary the service publishes.
_RELEASE_TYPE_TOKENS = (
    ("1", "Premiere"), ("2", "Limited theatrical"), ("3", "Theatrical"),
    ("4", "Digital"), ("5", "Physical"), ("6", "TV"),
)

# ISO 639-1 codes, lower-case like every other token here (a record states its
# language upper-case and the filter folds). The languages this app's calendars
# carry most, measured on one October's premieres — English, Japanese, Chinese
# and Korean lead the shows, Spanish, French and Portuguese the films — and
# everything else is one keystroke away.
_LANGUAGE_TOKENS = (
    ("en", "English"), ("es", "Spanish"), ("fr", "French"), ("de", "German"),
    ("it", "Italian"), ("pt", "Portuguese"), ("nl", "Dutch"), ("da", "Danish"),
    ("sv", "Swedish"), ("no", "Norwegian"), ("ja", "Japanese"), ("ko", "Korean"),
    ("zh", "Chinese"), ("hi", "Hindi"), ("th", "Thai"), ("ru", "Russian"),
)

# The missing-data rules, in the order the panel lists them — filter.py's
# MISSING_RULES says what each one hides.
_MISSING_TOKENS = (
    ("country", "No country"), ("language", "No language"), ("poster", "No poster"),
    ("country+language", "No country and no language"),
    ("country+poster", "No country and no poster"),
    ("language+poster", "No language and no poster"),
)

VOCABULARY: dict[str, tuple[Chip, ...]] = {
    TV_GENRES: _chips(*((t, _GENRE_LABELS[t]) for t in _TV_GENRE_TOKENS)),
    MOVIE_GENRES: _chips(*((t, _GENRE_LABELS[t]) for t in _MOVIE_GENRE_TOKENS)),
    TV_COUNTRIES: _chips(*_COUNTRY_TOKENS),
    MOVIE_COUNTRIES: _chips(*_COUNTRY_TOKENS),
    TV_LANGUAGES: _chips(*_LANGUAGE_TOKENS),
    MOVIE_LANGUAGES: _chips(*_LANGUAGE_TOKENS),
    # A CLOSED VOCABULARY: the type is one of the catalogue's own few words.
    TV_SHOW_TYPES: _chips(*SHOW_TYPES.items()),
    # NO VOCABULARY FOR KEYWORDS, for the networks' reason below: tens of
    # thousands exist, so the panel draws what the viewer has named and a box.
    TV_KEYWORDS: (),
    MOVIE_KEYWORDS: (),
    TV_MISSING: _chips(*_MISSING_TOKENS),
    MOVIE_MISSING: _chips(*_MISSING_TOKENS),
    MOVIE_RELEASE_COUNTRIES: _chips(*_RELEASE_MARKET_TOKENS),
    MOVIE_RELEASE_TYPES: _chips(*_RELEASE_TYPE_TOKENS),
    SHOW_CERTIFICATIONS: _chips(
        ("TV-Y", "TV-Y"), ("TV-Y7", "TV-Y7"), ("TV-G", "TV-G"), ("TV-PG", "TV-PG"),
        ("TV-14", "TV-14"), ("TV-MA", "TV-MA"), ("NR", "NR"),
    ),
    MOVIE_CERTIFICATIONS: _chips(
        ("G", "G"), ("PG", "PG"), ("PG-13", "PG-13"), ("R", "R"),
        ("NC-17", "NC-17"), ("NR", "NR"),
    ),
    # NO VOCABULARY FOR NETWORKS, and the empty tuple is the statement rather
    # than an omission. There are thousands, their spelling is load-bearing
    # (parse_network_spec: 'TVN' and 'tvN' are different broadcasters), and the
    # fast way to filter one has always been to press its badge on a card. The
    # panel draws whatever the viewer has already named and a box to add more.
    NETWORK_FILTER: (),
}

# Every CHIP field the panel can carry, which is also every chip field its save
# may write. The route validates against this rather than against a list of its
# own. The switches are TOGGLE_FIELDS, posted under a name of their own.
FIELDS = frozenset(VOCABULARY)

# Which fields belong to which tab. Certifications, networks and the release
# pair were already per-medium before the panel split; genres and countries
# became so with it. Show type and the no-network rule are the TV tab's alone:
# a film has neither a type nor a network.
TV_FIELDS = (TV_GENRES, TV_COUNTRIES, TV_LANGUAGES, NETWORK_FILTER, SHOW_CERTIFICATIONS,
             TV_SHOW_TYPES, TV_KEYWORDS, TV_MISSING, TV_SINGLE_SERVICE,
             TV_SPARSE_NO_NETWORK)
MOVIE_FIELDS = (MOVIE_GENRES, MOVIE_COUNTRIES, MOVIE_LANGUAGES, MOVIE_CERTIFICATIONS,
                MOVIE_RELEASE_COUNTRIES, MOVIE_RELEASE_TYPES, MOVIE_KEYWORDS,
                MOVIE_MISSING, MOVIE_SINGLE_SERVICE)


def empty(field: str):
    """What `field` holds when nothing narrows it — what Clear draws, and what a
    dimension mentioned with nothing set is saved as."""
    if field in TOGGLE_FIELDS:
        return False
    return [] if field in LIST_FIELDS else ""


_PREFIX = "chip"
_SEPARATOR = ":"

# The other two things the panel's payload says, each with a prefix of its own so
# one parser per shape can tell them apart without guessing.
#
# `field:<pref column>` IS "THIS DIMENSION WAS ON THE PANEL", and it is what makes
# "absent means leave alone" a rule the markup keeps rather than one that happens
# to hold. A dimension whose vocabulary is empty and whose stored spec is empty --
# networks, for an account that has never named one -- draws no chip at all, so
# without this marker the payload would not mention it and the writer could not
# tell "shown and empty" from "on a tab nobody opened". The marker costs one
# hidden input per dimension and turns that into something a test can assert.
#
# `source:<medium>:<service>` IS ONE SERVICE SWITCH. One field per service rather
# than one field carrying a joined list, because the panel posts as JSON and the
# encoder turns a name that appears twice into an ARRAY -- the one shape this
# app's form payloads deliberately never rely on (see this module's docstring).
# Each box has a hidden companion of the same name, so an unticked service says
# "0" instead of saying nothing.
_PRESENT_PREFIX = "field"
_SOURCE_PREFIX = "source"


def field_name(field: str, token: str) -> str:
    """The form-field name one chip posts under.

    A token containing the separator would produce a name that reads as a
    different field, so it is refused here rather than silently mangled. No
    vocabulary token contains one, and the only free text that reaches this is a
    network name — where a colon is rare but a viewer could type one, and the
    honest answer is to not offer them a control that would misfile it.
    """
    if _SEPARATOR in token:
        raise ValueError(f"a filter token may not contain {_SEPARATOR!r}: {token!r}")
    return f"{_PREFIX}{_SEPARATOR}{field}{_SEPARATOR}{token}"


def addable(token: str) -> bool:
    """Whether a token can be carried by a field name at all.

    The one thing a token cannot contain is the separator, and the reason is
    `field_name`'s: it would produce a name that reads as a different field. Asked
    as a question rather than caught as an exception because the panel asks it of
    every token in a pasted list and drops the ones it cannot carry.
    """
    return bool(token) and _SEPARATOR not in token


def parse_field_name(name: str) -> tuple[str, str] | None:
    """(field, token) for a chip field name, or None for anything else.

    Anything unrecognized returns None rather than raising: the payload is a
    whole form, it legitimately carries fields that are not chips (the pause
    switch, the service boxes), and a name from a newer version of the panel
    must not stop an older server saving the rest.
    """
    parts = str(name or "").split(_SEPARATOR, 2)
    if len(parts) != 3 or parts[0] != _PREFIX:
        return None
    field, token = parts[1], parts[2].strip()
    if field not in FIELDS or not token:
        return None
    return field, token


def presence_name(field: str) -> str:
    """The form-field name that says "this dimension was on the panel"."""
    return f"{_PRESENT_PREFIX}{_SEPARATOR}{field}"


def parse_presence_name(name: str) -> str | None:
    """The pref column a presence marker names, or None for anything else."""
    parts = str(name or "").split(_SEPARATOR)
    if len(parts) != 2 or parts[0] != _PRESENT_PREFIX or parts[1] not in FIELDS:
        return None
    return parts[1]


def source_field_name(media: Media, source: str) -> str:
    """The form-field name one service switch posts under.

    The MEDIUM is in the name because the two tabs answer separately -- one
    service's film calendar is every release in every market and the other's is
    twenty-five titles, so which services fill a calendar is a question worth
    asking once per medium.
    """
    return f"{_SOURCE_PREFIX}{_SEPARATOR}{media}{_SEPARATOR}{source}"


def sources_from_form(data: dict, media: Media) -> set[str] | None:
    """Which services this payload leaves switched on for `media`.

    None means the panel did not offer the control at all -- an instance with
    fewer than two services that could fill the medium draws nothing, and a
    payload that mentions no switch is not a viewer's answer. An EMPTY SET is a
    real answer and a refusable one: it is every box unticked, which the route
    turns down rather than storing.

    A ticked box and its hidden companion share a name, so the value can arrive
    as a list; `route_params.ticked` reads the last entry, which is the box
    overriding its companion.
    """
    prefix = f"{_SOURCE_PREFIX}{_SEPARATOR}{media}{_SEPARATOR}"
    chosen: set[str] = set()
    mentioned = False
    for name, value in data.items():
        if not str(name).startswith(prefix):
            continue
        source = str(name)[len(prefix):].strip()
        if not source:
            continue
        mentioned = True
        if route_params.ticked(value):
            chosen.add(source)
    return chosen if mentioned else None


# `toggle:<pref column>` IS ONE SWITCH (TOGGLE_FIELDS), with a hidden companion
# posting "0" under the same name, for the service switches' reason: an unticked
# box sends nothing, and absent means LEAVE ALONE to the save.
_TOGGLE_PREFIX = "toggle"


def toggle_name(field: str) -> str:
    """The form-field name one switch posts under."""
    return f"{_TOGGLE_PREFIX}{_SEPARATOR}{field}"


def toggles_from_form(data: dict) -> dict[str, bool]:
    """The switches this payload mentions, keyed by pref column — only those,
    so a tab nobody opened is left alone. The box and its companion share a name,
    so `route_params.ticked` reads the last entry, the box overriding."""
    out: dict[str, bool] = {}
    for name, value in data.items():
        parts = str(name or "").split(_SEPARATOR)
        if len(parts) == 2 and parts[0] == _TOGGLE_PREFIX and parts[1] in TOGGLE_FIELDS:
            out[parts[1]] = route_params.ticked(value)
    return out


@dataclass(frozen=True)
class ChipState:
    """A chip as the panel draws it: what it matches, what it says, how it is
    currently set, and whether the vocabulary named it."""
    token: str
    label: str
    mode: str
    name: str
    known: bool


def chips_for(field: str, spec) -> list[ChipState]:
    """Every chip to draw for `field`, given this viewer's stored spec.

    THE VOCABULARY FIRST, IN ITS DECLARED ORDER, then whatever else the spec
    holds, in the order it holds it. A viewer's own additions therefore sit
    together at the end rather than scattered through an alphabet they had no
    part in, and the vocabulary's order does not shuffle as answers change.

    A stored token that the vocabulary DOES name is drawn once, as that
    vocabulary chip, lit. Drawing it twice — once grey in the grid and once lit
    at the end — is the failure this ordering exists to prevent, and it is what
    a naive "vocabulary, then everything stored" would do.
    """
    is_list = field in LIST_FIELDS
    includes, excludes = parse(field, spec)
    # The comparison below has to fold exactly the way the matching parser folds
    # — otherwise a genre stored as "Drama" would draw an extra chip beside the
    # vocabulary's.
    def key(token: str) -> str:
        return token if field in CASED_FIELDS else token.lower()

    modes: dict[str, str] = {}
    for token in includes:
        modes[key(token)] = INCLUDE
    for token in excludes:
        modes[key(token)] = EXCLUDE

    out: list[ChipState] = []
    seen: set[str] = set()
    for chip in VOCABULARY.get(field, ()):
        seen.add(key(chip.token))
        out.append(ChipState(chip.token, chip.label, modes.get(key(chip.token), IGNORED),
                             field_name(field, chip.token), True))
    # Ordered by the spec rather than by the sets above, which are unordered and
    # would draw a viewer's own tokens in a different order on every page load.
    for token in _spec_order(spec, is_list):
        if key(token) in seen:
            continue
        seen.add(key(token))
        out.append(ChipState(token, token, modes.get(key(token), IGNORED),
                             field_name(field, token), False))
    return out


def _spec_order(spec, is_list: bool) -> list[str]:
    """The bare tokens of a spec, in the order it states them."""
    parts = ([str(p) for p in (spec or ())] if is_list
             else str(spec or "").split(","))
    out: list[str] = []
    for raw in parts:
        token = raw.strip()
        if token.startswith("-"):
            token = token[1:].strip()
        if token:
            out.append(token)
    return out


def specs_from_form(data: dict) -> dict[str, object]:
    """The stored specs a submitted panel implies, keyed by pref field.

    ONLY FIELDS THE PAYLOAD MENTIONS, so a panel that draws one tab does not
    silently clear the other. A tab is rendered with every one of its chips
    present as a field — including the ones set to "" — so a dimension the
    viewer cleared arrives as a field with an empty value and is written empty,
    while a dimension on the tab they never opened is absent and left alone.

    A DIMENSION CAN BE MENTIONED WITHOUT A SINGLE CHIP, which is why the presence
    marker exists: networks offer no vocabulary, so an account that has never
    named one draws an empty field and would otherwise be indistinguishable from
    a tab nobody opened. `field:<column>` says the panel showed it; the field is
    then written, empty unless a chip fills it.

    BUILT WITH filter.py's OWN MERGE, one token at a time, starting from empty.
    That is slower than joining strings and it is the point: the leading '-', the
    case rules and the list-versus-string difference stay stated in exactly one
    place, and a spec this function writes is byte-identical to one a card badge
    would have written.
    """
    out: dict[str, object] = {}
    for name in data:
        field = parse_presence_name(name)
        if field is not None:
            out.setdefault(field, empty(field))
    for name, value in data.items():
        parsed = parse_field_name(name)
        if parsed is None:
            continue
        field, token = parsed
        mode = str(value or "")
        if mode not in MODES:
            continue
        if field not in out:
            out[field] = empty(field)
        if mode is IGNORED or not mode:
            continue
        out[field] = calendar_filter.merge_token(out[field], token, mode,
                                                 is_list=field in LIST_FIELDS,
                                                 cased=field in CASED_FIELDS)
    return out


# ---- What a card's own badges write into ----

# The dimension a badge names, and the pref column it writes for each medium. It
# lives here rather than in the route because TWO things now ask it: the route
# that stores a press, and the card that draws the badge already carrying what is
# stored. A dimension named in one of those and not the other is a badge that
# saves somewhere it never reads back from.
#
# EVERY DIMENSION NEEDS THE MEDIUM, where once only certification did: a genre
# excluded from a film card is a statement about films, and writing it into the
# show calendar's genres is the conflation the two tabs exist to end. A film has
# no network at all, so a network badge can only have been pressed on a show card
# and there is one column it can mean.
BADGE_FIELDS: dict[str, dict[str, str]] = {
    "genre": {"movie": MOVIE_GENRES, "show": TV_GENRES},
    "country": {"movie": MOVIE_COUNTRIES, "show": TV_COUNTRIES},
    "language": {"movie": MOVIE_LANGUAGES, "show": TV_LANGUAGES},
    "network": {"movie": NETWORK_FILTER, "show": NETWORK_FILTER},
    "certification": {"movie": MOVIE_CERTIFICATIONS, "show": SHOW_CERTIFICATIONS},
}


def badge_key(dimension: str, token: str) -> str:
    """A selector-safe name for one badge, which is what an ANSWER addresses.

    A SECOND SPELLING OF THE SAME FACT, DERIVED, and the reason is the one
    `data-library-id` already carries on a library button: the badge also states
    its dimension and its matching token in the clear, because those are what a
    reader and a test want to see, but neither can be put inside a CSS attribute
    selector. A network name is free catalogue text — an apostrophe, a bracket or
    a backslash in one would turn the selector the answer is addressed with into
    a broken selector or a different one. Percent-encoded, every token comes out
    of the unreserved set, and one function spells the key on the badge and on
    the answer that replaces it.
    """
    return f"{quote(str(dimension), safe='')}.{quote(str(token), safe='')}"


def badge_field(dimension: str, media) -> str | None:
    """The pref column a badge press writes, or None for a dimension this app
    does not filter on. A card also draws a weekday, and storing it would be a
    preference that could never act."""
    columns = BADGE_FIELDS.get(str(dimension or ""))
    if columns is None:
        return None
    return columns["movie" if str(media or "") == "movie" else "show"]


class BadgeModes:
    """How each of this viewer's filter dimensions is currently set, in the form
    a card badge asks the question.

    BUILT ONCE PER RENDER, because a month draws thousands of badges and each
    would otherwise re-parse the same spec. The parsers are `filter.py`'s own, so
    a badge drawn as "filtered out" and a month that removed the title are
    reading one answer — and the badge a press is ANSWERED with is drawn by this
    same class, so the card and the answer cannot disagree.
    """

    def __init__(self, prefs: dict) -> None:
        self._modes: dict[str, dict[str, str]] = {}
        for field in {column for columns in BADGE_FIELDS.values()
                      for column in columns.values()}:
            includes, excludes = parse(field, prefs.get(field))
            # Folded exactly the way the matching parser folds, or a genre stored
            # as "Drama" would leave the card's "drama" badge looking unset.
            fold = (lambda token: token) if field in CASED_FIELDS else (lambda token: token.lower())
            self._modes[field] = {
                **{fold(token): INCLUDE for token in includes},
                **{fold(token): EXCLUDE for token in excludes},
            }

    def mode(self, dimension: str, token: str, media) -> str:
        field = badge_field(dimension, media)
        if field is None:
            return IGNORED
        key = token if field in CASED_FIELDS else str(token).lower()
        return self._modes.get(field, {}).get(key, IGNORED)

    # Reached through here rather than as a template global because every
    # template that draws a badge already has this object and nothing else needs
    # it. Both are the server's answers ABOUT a badge.
    key = staticmethod(badge_key)


# ---- What a medium's filters actually are, at read time ----

# The read path takes one keyword per dimension and always has, so this is the
# shape every caller wants back. Named here rather than built inline at four call
# sites: which column answers `genres` for a film is exactly the fact the split
# introduced, and four copies of that fact is four places to get it wrong.
_EMPTY_SPECS: dict[str, object] = {
    "genres": "", "countries": "",
    "show_certifications": "", "movie_certifications": "",
    "movie_release_countries": "", "movie_release_types": "",
    "network_filter": [],
    "languages": "", "keywords": [], "show_types": "",
    "single_service": False, "sparse_no_network": False, "missing": "",
}

# THE KEYWORDS THE READ PATH TAKES, in one tuple. Every function between a route
# and filter.py passes them through as one set (app/calendar/cache.py), and its
# memo key is built from this tuple — so a dimension added here reaches the key
# without anybody remembering to put it there.
SPEC_KEYS = tuple(_EMPTY_SPECS)


def complete_specs(specs: dict) -> dict[str, object]:
    """`specs` with every read keyword present, an absent one at its empty value.

    A KEYWORD THIS MODULE DOES NOT NAME IS A TypeError, as it would be against a
    signature that spelled each one out: the read path takes them as one set so
    that none can be left off a call, and a misspelt one silently filtering
    nothing would be the same failure arriving from the other side.
    """
    unknown = set(specs) - set(SPEC_KEYS)
    if unknown:
        raise TypeError(f"not a calendar filter: {', '.join(sorted(unknown))}")
    out = {**_EMPTY_SPECS, **specs}
    for key in ("network_filter", "keywords"):
        out[key] = list(out[key] or [])
    return out


def active_specs(prefs: dict, media: Media, *, honour_pause: bool) -> dict[str, object]:
    """This viewer's filters for one medium, as the read path's keywords.

    THE ONE PLACE A MEDIUM IS TURNED INTO COLUMNS. Every caller that filters a
    month — the calendar, its day fragments, the search, the share page — asks
    here, so "which genres apply to a film" has one answer rather than one per
    call site.

    A DIMENSION THE MEDIUM DOES NOT HAVE COMES BACK EMPTY RATHER THAN OMITTED,
    which is what keeps the read path's signature honest: it is handed every
    keyword every time and never has to distinguish "not asked" from "asked for
    nothing". Networks are the clearest case — a film has no network at all
    (the provider layer says so outright), so a film read is handed an empty
    network list rather than the viewer's show networks.

    `honour_pause` IS NOT A DEFAULT, AND THAT IS THE POINT. The stash is a
    private look at your own calendar: a viewer's own reads pass True, and a
    share link passes False so a paused session never quietly publishes a wider
    calendar to strangers. Making every caller state which it is means the share
    path had to think about it once, in writing, instead of inheriting an answer
    from a default nobody re-read.
    """
    if honour_pause and prefs.get("filters_paused"):
        return complete_specs({})
    if media is Media.MOVIE:
        return {
            **_EMPTY_SPECS,
            "genres": prefs.get(MOVIE_GENRES, ""),
            "countries": prefs.get(MOVIE_COUNTRIES, ""),
            "movie_certifications": prefs.get(MOVIE_CERTIFICATIONS, ""),
            "movie_release_countries": prefs.get(MOVIE_RELEASE_COUNTRIES, ""),
            "movie_release_types": prefs.get(MOVIE_RELEASE_TYPES, ""),
            "languages": prefs.get(MOVIE_LANGUAGES, ""),
            "keywords": list(prefs.get(MOVIE_KEYWORDS) or []),
            "single_service": bool(prefs.get(MOVIE_SINGLE_SERVICE)),
            "missing": prefs.get(MOVIE_MISSING, ""),
        }
    return {
        **_EMPTY_SPECS,
        "genres": prefs.get(TV_GENRES, ""),
        "countries": prefs.get(TV_COUNTRIES, ""),
        "show_certifications": prefs.get(SHOW_CERTIFICATIONS, ""),
        "network_filter": list(prefs.get(NETWORK_FILTER) or []),
        "languages": prefs.get(TV_LANGUAGES, ""),
        "keywords": list(prefs.get(TV_KEYWORDS) or []),
        "show_types": prefs.get(TV_SHOW_TYPES, ""),
        "single_service": bool(prefs.get(TV_SINGLE_SERVICE)),
        "sparse_no_network": bool(prefs.get(TV_SPARSE_NO_NETWORK)),
        "missing": prefs.get(TV_MISSING, ""),
    }


# THE SETTINGS FIELDS THE OPERATOR'S FLOOR IS READ FROM — every one, because the
# calendar's memo key carries the floor through `floor_specs` and nothing else
# (tests/calendar/test_span_memo.py holds each to moving it). Language and
# keywords are ONE floor for both media, like genres and countries: one
# vocabulary, one answer. Show type and the no-network rule are shows' alone.
# The single-service and missing-data floors are per medium, because the same
# rule weighs differently on each — "only one service lists it" hides about half
# of one October's series and nine films in ten. Those per-medium names are the
# viewer's columns, so the Settings panel draws them with the panel's own chips.
FLOOR_FIELDS = ("genres", "countries", "show_certifications", "movie_certifications",
                "network_filter", "languages", "keywords", TV_SHOW_TYPES,
                TV_SINGLE_SERVICE, MOVIE_SINGLE_SERVICE, TV_SPARSE_NO_NETWORK,
                TV_MISSING, MOVIE_MISSING)


def floor_specs(settings, media: Media) -> dict[str, object]:
    """The operator's floor for one medium, in `active_specs`' keywords — THE ONE
    PLACE A SETTINGS FIELD IS TURNED INTO WHAT A READ APPLIES, so the floor and a
    viewer's filters are judged by the same code (filter.TitleRules), in turn.

    THE NETWORK LIST IS HANDED TO A FILM READ TOO, which `active_specs` does not
    do for a viewer: the floor has always been one list applied to whichever
    calendar is read, so an include-only list leaves a film calendar empty, as it
    did before this function existed. The release specs are a viewer's alone and
    come back empty.
    """
    show = str(media) != str(Media.MOVIE)
    return {
        **_EMPTY_SPECS,
        "genres": settings.genres,
        "countries": settings.countries,
        "show_certifications": settings.show_certifications,
        "movie_certifications": settings.movie_certifications,
        "network_filter": list(settings.network_filter or []),
        "languages": settings.languages,
        "keywords": list(settings.keywords or []),
        "show_types": settings.tv_show_types if show else "",
        "single_service": bool(settings.tv_single_service if show
                               else settings.movie_single_service),
        "sparse_no_network": bool(settings.tv_sparse_no_network) if show else False,
        "missing": settings.tv_missing if show else settings.movie_missing,
    }


def title_rules(specs: dict, media) -> calendar_filter.TitleRules:
    """`specs` — `active_specs`' or `floor_specs`' keywords — parsed for a read of
    `media`: the certification spec for that medium, every other dimension as
    it stands. The release specs are not among them; they judge a film's
    per-service records before resolution (filter.filter_release_groups)."""
    return calendar_filter.TitleRules.parse(
        genres=specs.get("genres", ""), countries=specs.get("countries", ""),
        certifications=(specs.get("movie_certifications", "") if str(media) == str(Media.MOVIE)
                        else specs.get("show_certifications", "")),
        network_filter=specs.get("network_filter"),
        languages=specs.get("languages", ""), keywords=specs.get("keywords"),
        show_types=specs.get("show_types", ""),
        single_service=bool(specs.get("single_service")),
        sparse_no_network=bool(specs.get("sparse_no_network")),
        missing=specs.get("missing", ""))


def any_set(prefs: dict, fields) -> bool:
    """Whether any of `fields` is narrowing anything.

    Takes the fields rather than the medium so one implementation answers both
    "is this tab narrowed" (for the tab's count) and "is this calendar narrowed"
    (for the toolbar button), which are the same question asked of different
    sets.
    """
    return any(prefs.get(field) for field in fields)


def count_set(prefs: dict, fields) -> int:
    """How many individual tokens `fields` carry between them — the number a tab
    advertises.

    TOKENS, NOT DIMENSIONS. "3" on the Movies tab means three things are being
    filtered, which is what somebody glancing at it wants to know; counting
    dimensions would say "1" for a viewer who excluded nine genres and read as
    though almost nothing were set. A switch that is on is one thing filtered.
    """
    total = 0
    for field in fields:
        if field in TOGGLE_FIELDS:
            total += bool(prefs.get(field))
        else:
            total += len(_spec_order(prefs.get(field), field in LIST_FIELDS))
    return total
