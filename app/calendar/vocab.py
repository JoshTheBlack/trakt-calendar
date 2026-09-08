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

from ..providers.base import Media
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

# Networks are the one dimension stored as a LIST rather than a comma string,
# and filter.py's `merge_token` needs telling which it is being handed. Stated
# once here so no caller has to remember it.
LIST_FIELDS = frozenset({NETWORK_FILTER})

# The dimensions whose CASE is part of the token. Deliberately a second set even
# though it holds the same one field today: these two facts change for different
# reasons. Networks are a list because the column stores JSON; networks are
# case-sensitive because a single week of this calendar carried both 'TVN' and
# 'tvN' — a Polish broadcaster and a Korean one — and filter.py's
# parse_network_spec matches them exactly for that reason. A dimension could
# easily acquire one property without the other.
CASE_SENSITIVE_FIELDS = frozenset({NETWORK_FILTER})

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

VOCABULARY: dict[str, tuple[Chip, ...]] = {
    TV_GENRES: _chips(*((t, _GENRE_LABELS[t]) for t in _TV_GENRE_TOKENS)),
    MOVIE_GENRES: _chips(*((t, _GENRE_LABELS[t]) for t in _MOVIE_GENRE_TOKENS)),
    TV_COUNTRIES: _chips(*_COUNTRY_TOKENS),
    MOVIE_COUNTRIES: _chips(*_COUNTRY_TOKENS),
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

# Every field the panel can carry, which is also every field its save may write.
# The route validates against this rather than against a list of its own.
FIELDS = frozenset(VOCABULARY)

# Which fields belong to which tab. Certifications, networks and the release
# pair were already per-medium before the panel split; genres and countries
# became so with it.
TV_FIELDS = (TV_GENRES, TV_COUNTRIES, NETWORK_FILTER, SHOW_CERTIFICATIONS)
MOVIE_FIELDS = (MOVIE_GENRES, MOVIE_COUNTRIES, MOVIE_CERTIFICATIONS,
                MOVIE_RELEASE_COUNTRIES, MOVIE_RELEASE_TYPES)

_PREFIX = "chip"
_SEPARATOR = ":"


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
    includes, excludes = (
        calendar_filter.parse_network_spec(spec or ())
        if is_list else calendar_filter.parse_spec(str(spec or ""))
    )
    # parse_spec lowercases and parse_network_spec does not, so the comparison
    # below has to fold exactly the way the matching parser folds — otherwise a
    # genre stored as "Drama" would draw an extra chip beside the vocabulary's.
    def key(token: str) -> str:
        return token if is_list else token.lower()

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

    BUILT WITH filter.py's OWN MERGE, one token at a time, starting from empty.
    That is slower than joining strings and it is the point: the leading '-', the
    case rules and the list-versus-string difference stay stated in exactly one
    place, and a spec this function writes is byte-identical to one a card badge
    would have written.
    """
    out: dict[str, object] = {}
    for name, value in data.items():
        parsed = parse_field_name(name)
        if parsed is None:
            continue
        field, token = parsed
        mode = str(value or "")
        if mode not in MODES:
            continue
        is_list = field in LIST_FIELDS
        if field not in out:
            out[field] = [] if is_list else ""
        if mode is IGNORED or not mode:
            continue
        out[field] = calendar_filter.merge_token(out[field], token, mode, is_list=is_list)
    return out


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
}


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
        return dict(_EMPTY_SPECS)
    if media is Media.MOVIE:
        return {
            **_EMPTY_SPECS,
            "genres": prefs.get(MOVIE_GENRES, ""),
            "countries": prefs.get(MOVIE_COUNTRIES, ""),
            "movie_certifications": prefs.get(MOVIE_CERTIFICATIONS, ""),
            "movie_release_countries": prefs.get(MOVIE_RELEASE_COUNTRIES, ""),
            "movie_release_types": prefs.get(MOVIE_RELEASE_TYPES, ""),
        }
    return {
        **_EMPTY_SPECS,
        "genres": prefs.get(TV_GENRES, ""),
        "countries": prefs.get(TV_COUNTRIES, ""),
        "show_certifications": prefs.get(SHOW_CERTIFICATIONS, ""),
        "network_filter": list(prefs.get(NETWORK_FILTER) or []),
    }


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
    though almost nothing were set.
    """
    total = 0
    for field in fields:
        total += len(_spec_order(prefs.get(field), field in LIST_FIELDS))
    return total
