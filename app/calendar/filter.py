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
