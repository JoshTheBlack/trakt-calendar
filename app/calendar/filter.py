"""The read-time genre/country/certification/network filter for cached calendar
data.

Trakt used to apply `genres` and `countries` as query parameters, so its
calendar responses arrived already filtered server-side. The cache now stores
the complete unfiltered worldwide result instead — so that one viewer including
JP/KR shows and another excluding them are both servable from the same cached
bytes — and this predicate reproduces exactly what Trakt's server-side filter
did. It was checked item-for-item against the live API across both styles the
app produces (a leading-'-' exclude list and an allowlist), matching every time.

Filter on the RAW slugs the cached Record carries, BEFORE `base.render` rewrites
a genre like "game-show" into "Game Show" — the hyphenated slug is what the spec
matches against, so filtering after rendering would break every multi-word genre
while leaving single-word ones working, which is about the hardest failure of
this kind to spot.

Certification is a third, independent dimension on the same media object. It
is a scalar (one rating per item, like `country`, not a list like `genres`), so
it gets the same exclude-then-include treatment as country rather than the
set-intersection treatment genres gets.

NETWORK IS THE FOURTH, and it is deliberately kept apart from the other three
(parse_network_spec below says why). It also applies LATER: the three above run
on the resolved Record, while network runs on rendered items, because that is the
shape both callers hold at the point they filter.

THE RELEASE FILTER IS THE FIFTH AND IT IS A FILMS-ONLY DIMENSION, running
EARLIER than any of them — over a group's per-source records, before resolution
picks between them. It exists because one service's films calendar is a global
release calendar: every release in every market, measured at 1314 titles in one
real August against the other service's 25. Narrowing it needs the per-country
release schedule, which no calendar payload carries and only the per-title
catalogue does, so like the film prune it can act only on what enrichment has
already found. See filter_release_groups for the rule and for why it is asked
once per group rather than once per record.

SIX MORE ARE ASKED OF A CARD WHOLE (`keep_title`), because a catalogue calendar
lists everything anybody entered — TMDB took one October's Series Premieres
from 180 cards to 991. Language, keywords and a show's type judge the resolved
record like the three above; "only one service lists it", "only a sparse
catalogue lists it and it names no network" and "it is missing a fact" judge
the card against the services that listed it. `filter_titles` is the one place
all of them, and the four above, are applied to a card — the operator's floor
and a viewer's filters alike.
"""
from __future__ import annotations

from typing import Iterable, NamedTuple, Protocol, Sequence, TypeVar


def parse_spec(spec: str) -> tuple[set[str], set[str]]:
    """Split a `-anime,-music,drama` spec into (includes, excludes), lowercased.

    A leading '-' puts the bare token in EXCLUDES; every other token is an
    INCLUDE. Whitespace and empty tokens are ignored.
    """
    includes: set[str] = set()
    excludes: set[str] = set()
    for raw in (spec or "").split(","):
        token = raw.strip().lower()
        if not token:
            continue
        if token.startswith("-"):
            bare = token[1:].strip()
            if bare:
                excludes.add(bare)
        elif token:
            includes.add(token)
    return includes, excludes


def merge_token(spec, token: str, mode: str, *, is_list: bool = False,
                cased: bool | None = None):
    """`spec` with one token set to `mode` — "include", "exclude", or "" to drop
    it entirely. Returns the same shape it was given: a list for networks and
    keywords, a comma string for everything else. `cased` says whether two
    spellings differing only in case are different tokens — true for networks
    (below), false for keywords (parse_keyword_spec); it defaults to `is_list`,
    which is the networks' answer.

    IT LIVES BESIDE THE PARSERS BECAUSE IT WRITES WHAT THEY READ. The leading-'-'
    convention, which dimensions fold case and which must not, and the fact that
    networks are a list where the others are one string — every one of those is
    stated above, and a second statement of them anywhere else is a second thing
    to keep in step. A caller that offers to filter on something a card is
    showing needs to ADD to a spec, and this is the only place that knows how.

    ONE ANSWER PER TOKEN, so the spec can never hold a token twice or hold it
    both ways: any earlier mention is removed before the new one is appended.
    Choosing "only this" for something already excluded means the new answer
    rather than both, which is what somebody pressing it plainly means.

    MATCHED THE WAY THE MATCHING PARSER MATCHES. parse_spec lowercases, so a
    token already present under a different case is the same token and is
    replaced; parse_network_spec does not, because a single week of the calendar
    held both 'TVN' and 'tvN' — a Polish broadcaster and a Korean one — so for
    networks only an exact match is the same network.

    ORDER IS OTHERWISE PRESERVED and the new token goes last, so a spec somebody
    has been building up by hand reads in the order they built it.
    """
    token = str(token or "").strip()
    if not token:
        return spec
    if is_list:
        parts = [str(p).strip() for p in (spec or ()) if str(p).strip()]
    else:
        parts = [p.strip() for p in str(spec or "").split(",") if p.strip()]

    def bare(part: str) -> str:
        return (part[1:] if part.startswith("-") else part).strip()

    exact = is_list if cased is None else cased

    def same(a: str, b: str) -> bool:
        return a == b if exact else a.lower() == b.lower()

    kept = [p for p in parts if not same(bare(p), token)]
    if mode == "exclude":
        kept.append("-" + token)
    elif mode == "include":
        kept.append(token)
    return kept if is_list else ", ".join(kept)


def parse_keyword_spec(keywords: Iterable[str] | None) -> tuple[set[str], set[str]]:
    """Split keyword names into (includes, excludes), lower-cased.

    A LIST LIKE THE NETWORKS AND FOLDED LIKE THE GENRES, and each half for its
    own reason. A list, because a TMDB keyword's name can hold a comma ("san
    francisco, california"), so a comma string would cut one keyword in two.
    Folded, because TMDB spells every keyword in lower case (normalize.keywords
    stores them so) and a viewer typing "Anime" means "anime".
    """
    includes: set[str] = set()
    excludes: set[str] = set()
    for raw in keywords or ():
        name = " ".join(str(raw).split()).lower()
        if not name:
            continue
        if name.startswith("-"):
            bare = name[1:].strip()
            if bare:
                excludes.add(bare)
        else:
            includes.add(name)
    return includes, excludes


def parse_network_spec(networks: Iterable[str] | None) -> tuple[set[str], set[str]]:
    """Split network names into (includes, excludes), CASE PRESERVED.

    Same leading-'-' convention as parse_spec, and deliberately NOT the same
    function. parse_spec lowercases both sides, which is right for the three
    dimensions it serves: genre slugs, two-letter country codes and
    certifications are closed vocabularies Trakt spells exactly one way.

    A network name is free text, spelled however the network spells itself, and
    the case carries meaning. A single week of the calendar came back holding
    both 'TVN' and 'tvN' — a Polish broadcaster and a Korean one — alongside
    'Discovery', 'Discovery Channel' and 'discovery+', and 'tv asahi' next to
    'TV Tokyo'. Folding case would silently merge networks that are not the same
    network, so matching stays exact, as it has since this filter was added.

    Networks arrive as a LIST (the textarea's commas are split before storage,
    see routes._network_list), which is why this takes an iterable
    where parse_spec takes one string.
    """
    includes: set[str] = set()
    excludes: set[str] = set()
    for raw in networks or ():
        name = str(raw).strip()
        if not name:
            continue
        if name.startswith("-"):
            bare = name[1:].strip()
            if bare:
                excludes.add(bare)
        else:
            includes.add(name)
    return includes, excludes


def keep_network(network: str, inc: set[str], exc: set[str]) -> bool:
    """Whether one item's network survives the parsed network spec.

    Exclude-first, like every other dimension here. An item with NO network is
    KEPT under an exclude-only spec and DROPPED under an include spec, the same
    way a missing country or certification behaves — and it is not a rare case:
    Trakt's movie objects carry no network field at all, so every film answers
    the empty string.
    """
    if exc and network in exc:
        return False
    if inc and network not in inc:
        return False
    return True


class _HasNetwork(Protocol):
    network: str
    enriched: bool


ItemT = TypeVar("ItemT", bound=_HasNetwork)


def filter_by_network(items: Sequence[ItemT], networks: Iterable[str] | None,
                      *, exempt_unenriched: bool = False) -> list[ItemT]:
    """Keep the normalized `items` whose network survives `networks`.

    Takes anything carrying `network` and `enriched`, which is both shapes this
    is asked about: a Record at fill (cache.py's instance floor, before a window
    is stored) and a normalized item at read (the same floor again, then the
    viewer's own list). `network` survives normalization unchanged, so one
    predicate answers for both rather than a second one written per shape. An
    empty spec is a pass-through.

    `exempt_unenriched` is the same exemption keep_record applies, said here
    because network is filtered separately and later (see the module
    docstring) — an unenriched item's blank network must not be judged as "no
    network" any more than its blank genres should be judged as "no genres".
    """
    inc, exc = parse_network_spec(networks)
    if not (inc or exc):
        return list(items)
    return [item for item in items
            if (exempt_unenriched and not item.enriched) or keep_network(item.network, inc, exc)]


def parse_release_type_spec(spec: str) -> tuple[set[int], set[int]]:
    """Split a release-type spec into (includes, excludes) as NUMBERS.

    The vocabulary is TMDB's numbering, which Simkl reproduces verbatim — see
    app/providers/simkl/titles.py's RELEASE_TYPE_LABELS for the names. The spec
    is stored as those numbers rather than as words because they are what the
    service publishes: a stored "theatrical" would need a translation table
    sitting between the preference and the payload, and a seventh type would
    then be unnameable until somebody edited that table.

    A token that is not a number is DROPPED rather than refused. This runs on
    the read path over a stored value, where the rule everywhere else in this
    package is that a preference a later version wrote must not stop a page
    rendering; the write path is where a bad value is refused (see
    app/calendar/routes.py).
    """
    raw_inc, raw_exc = parse_spec(spec)

    def numbers(tokens: set[str]) -> set[int]:
        out: set[int] = set()
        for token in tokens:
            try:
                out.add(int(token))
            except ValueError:
                continue
        return out

    return numbers(raw_inc), numbers(raw_exc)


def keep_values(genres, country, certification,
                g_inc: set[str], g_exc: set[str],
                c_inc: set[str], c_exc: set[str],
                cert_inc: set[str], cert_exc: set[str]) -> bool:
    """Whether one title's three values survive the parsed genre/country/
    certification spec. THE PREDICATE, said once.

    It takes the three VALUES rather than the object carrying them because the
    same rule has to answer for a Record and for a raw payload captured from the
    live API, and a version per shape is a version that can drift from the other.
    Everything is lowercased here, so a caller may hand over whatever spelling it
    holds — the display form of a country is upper-case and the spec is not.

    The dimensions are independent and each is checked exclude-first: an
    exclude hit drops the item; an include list drops anything not in it. An
    item with no genres is therefore KEPT under an exclude-only genre spec (its
    empty set intersects nothing) and DROPPED under an include genre spec (it is
    in nothing) — which is what Trakt itself does. Certification follows the
    country precedent, not the genre one: it is a single scalar, missing on
    roughly a third of shows (mostly non-US content with no US TV rating), so a
    missing certification is KEPT under exclude-only and DROPPED under include,
    same as a missing country.
    """
    genre_set = {str(g).lower() for g in (genres or ())}
    country = str(country or "").lower()
    certification = str(certification or "").lower()
    if g_exc and (genre_set & g_exc):
        return False
    if g_inc and not (genre_set & g_inc):
        return False
    if c_exc and country in c_exc:
        return False
    if c_inc and country not in c_inc:
        return False
    if cert_exc and certification in cert_exc:
        return False
    if cert_inc and certification not in cert_inc:
        return False
    return True


def keep_record(record, g_inc: set[str], g_exc: set[str],
                c_inc: set[str], c_exc: set[str],
                cert_inc: set[str], cert_exc: set[str],
                *, exempt_unenriched: bool = False) -> bool:
    """keep_values, asked of a Record.

    `exempt_unenriched` lets a record that has not been enriched yet survive
    regardless of what the spec says, rather than being judged on values it
    cannot yet answer for — see Record.enriched. It is OFF by default and set by
    both of app/calendar/cache.py's READS: the instance floor and the per-viewer
    layer that follows it (`visible_records`). The instance floor's OTHER
    application — at FILL time, in cache.py's fetch_window_records — deliberately
    does NOT set it: a Simkl record is always unenriched at that point
    (enrichment only ever overlays at read, never at fill; see
    app/calendar/enrich.py), so exempting it there would make the floor a
    permanent no-op for every Simkl title rather than a temporary gap that closes
    once enrichment catches up. The fill keeps judging what it is given; the
    reads a viewer actually sees give the grace period, so a title whose genres
    are not known yet is shown rather than hidden for something nobody has
    established about it.
    """
    if exempt_unenriched and not record.enriched:
        return True
    return keep_values(record.genres, record.country, record.certification,
                       g_inc, g_exc, c_inc, c_exc, cert_inc, cert_exc)


def filter_records(records, genres_spec: str, countries_spec: str,
                   certifications_spec: str = "", *, exempt_unenriched: bool = False) -> list:
    """Keep the `records` that pass all three specs.

    Certification vocabularies differ between shows and movies (TV Parental
    Guidelines vs. MPA ratings), so it is the caller's job to pass the spec
    matching the endpoint's media kind, exactly as it already picks the endpoint.
    An empty set of specs is a fast pass-through (nothing to filter on) —
    EVEN WHEN `exempt_unenriched` IS SET, because an unenriched record survives
    an empty spec exactly as an enriched one does; the exemption only matters
    once there is something to be exempt FROM.

    `exempt_unenriched` is passed straight through to keep_record — see there
    for which callers set it and why the fill does not.
    """
    g_inc, g_exc = parse_spec(genres_spec)
    c_inc, c_exc = parse_spec(countries_spec)
    cert_inc, cert_exc = parse_spec(certifications_spec)
    if not (g_inc or g_exc or c_inc or c_exc or cert_inc or cert_exc):
        return list(records)
    return [r for r in records
            if keep_record(r, g_inc, g_exc, c_inc, c_exc, cert_inc, cert_exc,
                           exempt_unenriched=exempt_unenriched)]


def keep_languages(records, languages_spec: str) -> list:
    """The `records` whose own language survives `languages_spec`, judged on the
    record ALONE and with no exemption — the fill's half of the language floor,
    asked beside `filter_records` for the reason that function gives for not
    exempting at fill. A record with no language is dropped under an include
    spec and kept under an exclude one, exactly as a missing country is."""
    inc, exc = parse_spec(languages_spec)
    if not (inc or exc):
        return list(records)
    return [r for r in records if _keep_scalar(r.language, inc, exc)]


def _keep_scalar(value, inc: set[str], exc: set[str]) -> bool:
    """One scalar value against an (includes, excludes) pair, exclude first —
    the rule country and certification follow in `keep_values`."""
    value = str(value or "").lower()
    if exc and value in exc:
        return False
    if inc and value not in inc:
        return False
    return True


# ---------------------------------------------------------------------------
# a card, judged whole
# ---------------------------------------------------------------------------

# WHAT A "MISSING DATA" RULE NAMES: each ticked rule hides a card missing EVERY
# fact it lists. Six rules rather than three facts and a mode, because the
# pairs are what trims a catalogue's long tail without the cost of either half:
# measured on one October's films, "no country" alone hides 1,805 cards and "no
# country and no poster" 292. The tokens are what a stored spec holds.
MISSING_RULES: dict[str, tuple[str, ...]] = {
    "country": ("country",),
    "language": ("language",),
    "poster": ("poster",),
    "country+language": ("country", "language"),
    "country+poster": ("country", "poster"),
    "language+poster": ("language", "poster"),
}


def parse_missing_spec(spec: str) -> tuple[tuple[str, ...], ...]:
    """The ticked rules a stored spec names, each as the facts it needs missing.
    A token no rule names is dropped rather than refused, as a release type is
    (`parse_release_type_spec`): this runs on the read path over a stored value."""
    includes, _excludes = parse_spec(spec)
    return tuple(MISSING_RULES[token] for token in sorted(includes) if token in MISSING_RULES)


class TitleRules(NamedTuple):
    """One set of specs, parsed once for a whole read — the operator's floor, or
    a viewer's filters for one medium (app/calendar/vocab.py's `floor_specs` and
    `active_specs` give the keywords `parse` takes)."""
    genres: tuple[set[str], set[str]]
    countries: tuple[set[str], set[str]]
    certifications: tuple[set[str], set[str]]
    networks: tuple[set[str], set[str]]
    languages: tuple[set[str], set[str]]
    keywords: tuple[set[str], set[str]]
    show_types: tuple[set[str], set[str]]
    single_service: bool
    sparse_no_network: bool
    missing: tuple[tuple[str, ...], ...]

    @classmethod
    def parse(cls, *, genres: str = "", countries: str = "", certifications: str = "",
              network_filter=None, languages: str = "", keywords=None, show_types: str = "",
              single_service: bool = False, sparse_no_network: bool = False,
              missing: str = "") -> "TitleRules":
        return cls(parse_spec(genres), parse_spec(countries), parse_spec(certifications),
                   parse_network_spec(network_filter), parse_spec(languages),
                   parse_keyword_spec(keywords), parse_spec(show_types),
                   bool(single_service), bool(sparse_no_network),
                   parse_missing_spec(missing))

    def narrows(self) -> bool:
        return (any(inc or exc for inc, exc in (self.genres, self.countries,
                                                 self.certifications, self.networks,
                                                 self.languages, self.keywords,
                                                 self.show_types))
                or self.single_service or self.sparse_no_network or bool(self.missing))


