"""The read-time genre/country/certification filter for cached calendar data.

Trakt used to apply `genres` and `countries` as query parameters, so its
calendar responses arrived already filtered server-side. The cache now stores
the complete unfiltered worldwide result instead — so that one viewer including
JP/KR shows and another excluding them are both servable from the same cached
bytes — and this predicate reproduces exactly what Trakt's server-side filter
did. It was checked item-for-item against the live API across both styles the
app produces (a leading-'-' exclude list and an allowlist), matching every time.

Filter on the RAW slugs straight from the cached blob, BEFORE the normalizer
rewrites a genre like "game-show" into "Game Show" — the hyphenated slug is what
the spec matches against, so filtering after normalization would break every
multi-word genre.

Certification is a third, independent dimension on the same media object. It
is a scalar (one rating per item, like `country`, not a list like `genres`), so
it gets the same exclude-then-include treatment as country rather than the
set-intersection treatment genres gets.
"""
from __future__ import annotations


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


def keep_media(media: dict, g_inc: set[str], g_exc: set[str],
               c_inc: set[str], c_exc: set[str],
               cert_inc: set[str], cert_exc: set[str]) -> bool:
    """Whether one raw media object survives the parsed genre/country/
    certification spec.

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
    genres = {str(g).lower() for g in (media.get("genres") or [])}
    country = str(media.get("country") or "").lower()
    certification = str(media.get("certification") or "").lower()
    if g_exc and (genres & g_exc):
        return False
    if g_inc and not (genres & g_inc):
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


def filter_entries(entries, media_key: str, genres_spec: str, countries_spec: str,
                    certifications_spec: str = "") -> list[dict]:
    """Keep the calendar `entries` whose media object passes all three specs.

    `media_key` is the endpoint's media key ('show' | 'movie') — the key under
    which each entry carries its media object. Certification vocabularies
    differ between shows and movies (TV Parental Guidelines vs. MPA ratings),
    so it is the caller's job to pass the spec matching `media_key`, the same
    way the caller already picks `media_key` itself. An empty set of specs is a
    fast pass-through (nothing to filter on).
    """
    g_inc, g_exc = parse_spec(genres_spec)
    c_inc, c_exc = parse_spec(countries_spec)
    cert_inc, cert_exc = parse_spec(certifications_spec)
    if not (g_inc or g_exc or c_inc or c_exc or cert_inc or cert_exc):
        return list(entries)
    return [
        entry for entry in entries
        if keep_media(entry.get(media_key) or {}, g_inc, g_exc, c_inc, c_exc, cert_inc, cert_exc)
    ]
