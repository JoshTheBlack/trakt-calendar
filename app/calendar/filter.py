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
"""
from __future__ import annotations

from typing import Iterable, Protocol, Sequence, TypeVar


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

    Takes normalized items rather than raw entries because that is what the
    caller holds when it filters: cache.py's per-viewer read-time pass runs
    after the stored entries have been replayed through the provider's
    normalizer, and `network` only exists on the far side of that. An empty spec
    is a pass-through.

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


def keep_release_blocks(release_types_by_country, c_inc: set[str], c_exc: set[str],
                        t_inc: set[int], t_exc: set[int]) -> bool:
    """Whether one film's {country: [type]} map has a release the viewer asked
    for. THE PREDICATE, said once, over the values rather than the object.

    THE TWO DIMENSIONS ARE JUDGED ON THE SAME BLOCK, WHICH IS THE WHOLE RULE
    AND IS NOT WHAT ASKING THEM SEPARATELY WOULD DO. "US, theatrical" means a US
    block that is theatrical — not "has a US release" and, unrelatedly, "has a
    theatrical release somewhere". A film premiering in Brazil and reaching
    American cinemas satisfies both readings; a film premiering in America and
    reaching Brazilian cinemas satisfies only the loose one, and a viewer who
    asked for American theatrical releases did not ask for it. Measured over one
    live August: the joint reading takes 1314 films to 219 and the loose one
    would not.

    AN EXCLUDE DISQUALIFIES THE BLOCK, NOT THE FILM, and that is the difference
    from every other dimension in this module — those are scalars, where one
    excluded value is the title's only answer. Here a film has several answers,
    so `-br` means "a Brazilian release is not one I count"; a film out in both
    Brazil and America still survives on its American block, and only a
    Brazil-only film disappears.

    EMPTY IS NOT A DECISION. A film with no blocks at all cannot be judged, and
    the caller (keep_release) is what decides that it is kept rather than
    dropped — this function is asked only about a film that has something to
    say.
    """
    for country, types in (release_types_by_country or {}).items():
        code = str(country).lower()
        if c_exc and code in c_exc:
            continue
        if c_inc and code not in c_inc:
            continue
        for kind in types or ():
            if t_exc and kind in t_exc:
                continue
            if t_inc and kind not in t_inc:
                continue
            return True
    return False


def keep_release(record, c_inc: set[str], c_exc: set[str],
                 t_inc: set[int], t_exc: set[int]) -> bool:
    """keep_release_blocks, asked of a Record, with the "we do not know" case
    answered here.

    A RECORD CARRYING NO RELEASE MAP IS KEPT, ALWAYS. Three real situations
    produce one and none of them is "this film is released nowhere": a source
    whose calendar payload has no release schedule at all (Trakt's does not, so
    every Trakt record answers this way), a Simkl film whose enrichment has not
    landed yet, and a row written before the map was extracted. Dropping any of
    them would let a filter delete titles it has no information about — the same
    reasoning behind `exempt_unenriched` above, except that it needs no flag,
    because an empty map already says exactly "nothing to judge".

    THE COST OF THAT, STATED: a viewer who narrows to US theatrical still sees
    the curated handful of films the other service listed, because that service
    never says how a film is being released. That is the honest answer — this
    filter can only act on what somebody actually published.
    """
    blocks = getattr(record, "release_types_by_country", None)
    if not blocks:
        return True
    return keep_release_blocks(blocks, c_inc, c_exc, t_inc, t_exc)


def filter_release_groups(parsed, endpoint_media, countries_spec: str,
                          types_spec: str) -> list:
    """Keep the (group, records) pairs holding at least one record whose release
    survives the two specs. A MOVIE-ENDPOINT RULE and inert everywhere else.

    IT TAKES THE PAIRS THE READ PATH HOLDS AT THIS POINT rather than a flat list
    of records, and that is the decision, not a convenience. The question is
    which TITLES a viewer sees, so it has to be asked once per group: dropping
    individual records instead would leave a title both services listed rendering
    as a single-source card whenever one of them happened to be the one that
    could answer, quietly changing what the card says about its own provenance
    to enforce a filter about something else entirely.

    A GROUP SURVIVES IF ANY OF ITS RECORDS DOES, which follows from the same
    place: a record that cannot answer is kept (see keep_release), so a merged
    group always survives on its uninformed side. That is the honest reading —
    one service saying nothing about release formats is not evidence against
    what the other one said.

    RUNS AT READ, LIKE EVERY OTHER PER-VIEWER NARROWING HERE, and for the extra
    reason `prune_disguised_films` gives: the release map arrives only once the
    background enrichment drain has looked the title up, long after the window
    was stored. An unenriched film is therefore still shown until its row lands.
    """
    if str(endpoint_media) != "movie":
        return list(parsed)
    c_inc, c_exc = parse_spec(countries_spec)
    t_inc, t_exc = parse_release_type_spec(types_spec)
    if not (c_inc or c_exc or t_inc or t_exc):
        return list(parsed)
    return [pair for pair in parsed
            if any(keep_release(record, c_inc, c_exc, t_inc, t_exc)
                   for record in pair[1])]


def prune_disguised_films(records, endpoint_media) -> list:
    """Drop a record Simkl's own enrichment marks as a FILM from a SERIES
    endpoint (shows/new, shows/premieres, shows — anything whose media is
    "show", never a movie endpoint).

    THE SIGNAL IS `anime_type == "movie"`, MEASURED, NOT `total_episodes`.
    Two titles measured live carry `total_episodes: 1` — The Ribbon Hero, an
    ONA SERIES (Original Net Animation: an anime released to the web, not a
    film), and Shiranuhi, an actual film — and only `anime_type` tells them
    apart. `ona`, `ova`, `tv` and `special` are all SERIAL formats and must
    survive this check untouched; only `movie` is pruned. Across three
    disjoint samples totalling 300 titles this measured at 1.7% of entries
    (5 of 300), which is why this is one condition here rather than new
    machinery — see app/providers/simkl/titles.py's `_extract` for where the
    field is captured.

    THIS IS THE BACKSTOP AND NO LONGER THE ONLY RULE, WHICH CHANGES WHAT IT IS
    FOR. A Simkl anime film is now kept off the series endpoints at the FILL
    instead — app/providers/simkl/calendar.py's `is_anime_film` reads the
    `anime_type` the CALENDAR file states on the entry itself, so the entry
    never enters a show window and is normalized onto the movies one instead.
    What still reaches this function, and why it stays:

      - A WINDOW STORED BEFORE THAT SPLIT EXISTED, which still holds the film
        under a show key until its TTL turns it over. Without this the title
        would reappear on Series Premieres for one TTL on every instance that
        upgrades.
      - A TITLE THE CALENDAR FILE DOES NOT LABEL but the per-title detail
        payload does. Measured across five months of live archives every anime
        entry carried an `anime_type`, so this is a defence against Simkl's
        shape changing rather than against anything observed — but the field
        being absent is exactly the case where the fill cannot decide and this
        can.

    IT RUNS AT READ BECAUSE ITS OWN SIGNAL IS ENRICHMENT'S, arriving once the
    background drain (app/calendar/enrich.py) has looked the title up, long
    after the fill stored the window. THE TRANSITIONAL STATE THAT LEAVES, AND
    IT IS DECLARED RATHER THAN DISCOVERED: for a title in one of those two
    cases, nothing knows it is a film until enrichment lands, so it renders on
    the series calendar and NOT on the movies one; and once enrichment does
    land, this drops it from the series calendar while nothing puts it on the
    movies one until that month's movies window is next filled. Both halves
    are one TTL wide and self-healing, and both fail in the honest direction —
    the same shape the genre/country/certification exemption already accepts
    elsewhere in this module, not a defect to chase.
    """
    if str(endpoint_media) != "show":
        return list(records)
    return [r for r in records if getattr(r, "anime_type", "") != "movie"]


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
    cannot yet answer for — see Record.enriched. It is OFF by default and used
    in exactly one place: app/calendar/cache.py's per-viewer READ. The
    instance-wide content floor filter.filter_records applies at FILL time
    (cache.py's fetch_window_records) deliberately does NOT set it — a Simkl
    record is always unenriched at that point (enrichment only ever overlays
    at read, never at fill; see app/calendar/enrich.py), so exempting it there
    would make the floor a permanent no-op for every Simkl title rather than a
    temporary gap that closes once enrichment catches up. The floor keeps
    judging what it is given; only the read a viewer actually sees gets the
    grace period.
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
    for why only one caller sets it.
    """
    g_inc, g_exc = parse_spec(genres_spec)
    c_inc, c_exc = parse_spec(countries_spec)
    cert_inc, cert_exc = parse_spec(certifications_spec)
    if not (g_inc or g_exc or c_inc or c_exc or cert_inc or cert_exc):
        return list(records)
    return [r for r in records
            if keep_record(r, g_inc, g_exc, c_inc, c_exc, cert_inc, cert_exc,
                           exempt_unenriched=exempt_unenriched)]
