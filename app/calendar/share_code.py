"""The compact `?p=` code a share link carries instead of a long query string.

A fully specified share link spells out seven params, percent-encoding two of
them, and comes to something like

    /c/testing?endpoint=shows%2Fpremieres&card=horizontal&packing=stacked
               &hidenw=1&tz=America%2FNew_York&year=2026&month=8

which is unwieldy to paste anywhere people actually paste links. The same view
encodes to `/c/testing?p=13F1201412C3` — every option is an index into a fixed
vocabulary, and the indexes are packed together as hex.

    char 0      format version, "1"
    chars 1-2   presence bitmask: which params the code carries at all
    then, in bit order, only the params the mask claims:
        endpoint  1 hex   index into ENDPOINT_CODES
        card      1 hex   index into CARD_CODES
        packing   1 hex   index into PACKING_CODES
        hidenw    1 hex   0 or 1
        tz        2 hex   index into TZ_CODES
        year+month 3 hex  months since January 1970, the pair being one choice
        source    2 hex   BITMASK over SOURCE_CODES, 0 meaning "auto"

A code is only ever a way to HAND a link out: the public page expands it back
into ordinary query params and redirects there on arrival, so nothing else in
the app has to know this format exists (see share_routes._expanded_from_code).

Design rules, all three of which exist because these codes end up in links other
people hold on to:

1. The codebooks below are APPEND-ONLY. A position in one is a promise about what
   that number means — an index for most fields, a bit for `source` — and
   reordering or removing an entry silently repoints every link already in the
   wild at a different timezone, a different calendar or a different service.
   tests/calendar/test_share_code.py fails if a codebook is reordered or shortened, or if
   a vocabulary elsewhere in the app grows an entry that was never appended here.
2. Nothing here ever raises or half-decodes. A value this module cannot encode
   (an index too wide for its field, a timezone outside the codebook) makes
   `encode` return None and the caller falls back to the long, always-correct
   query string; a code this module cannot decode yields the params it could
   read and drops the rest, so a mangled `p=` degrades to the owner's defaults
   rather than to an error page.
3. A field that outgrows its width gets a NEW LAYOUT VERSION rather than a wider
   field in this one. _LAYOUTS is keyed by the version character, `encode` uses
   the newest and `decode` honours every version ever published, so a v1 link
   keeps meaning what it meant on the day it was copied. Until that day comes,
   rule 2 covers the overflow on its own: the link just goes out verbose.
"""
from __future__ import annotations

from ..sources import prefs as source_prefs

# What `encode` writes. `decode` reads this and every older layout in _LAYOUTS.
VERSION = "1"

# Append-only. See rule 1 above.
ENDPOINT_CODES = ("shows/new", "shows/premieres", "shows/finales", "shows", "movies")
CARD_CODES = ("vertical", "horizontal", "poster")
PACKING_CODES = ("stacked", "packed")
TZ_CODES = (
    "UTC", "Africa/Casablanca", "Africa/Lagos", "Africa/Algiers", "Africa/Tunis",
    "Africa/Cairo", "Africa/Johannesburg", "Africa/Nairobi", "Africa/Addis_Ababa",
    "Africa/Khartoum", "Africa/Accra", "Africa/Windhoek", "America/St_Johns",
    "America/Halifax", "America/New_York", "America/Toronto", "America/Havana",
    "America/Chicago", "America/Mexico_City", "America/Denver", "America/Phoenix",
    "America/Los_Angeles", "America/Vancouver", "America/Anchorage", "America/Bogota",
    "America/Lima", "America/Panama", "America/Caracas", "America/Santiago",
    "America/La_Paz", "America/Sao_Paulo", "America/Argentina/Buenos_Aires",
    "America/Montevideo", "Asia/Jerusalem", "Asia/Beirut", "Asia/Baghdad", "Asia/Riyadh",
    "Asia/Tehran", "Asia/Dubai", "Asia/Karachi", "Asia/Kolkata", "Asia/Kathmandu",
    "Asia/Colombo", "Asia/Dhaka", "Asia/Yangon", "Asia/Bangkok", "Asia/Ho_Chi_Minh",
    "Asia/Jakarta", "Asia/Singapore", "Asia/Kuala_Lumpur", "Asia/Hong_Kong",
    "Asia/Shanghai", "Asia/Taipei", "Asia/Manila", "Asia/Seoul", "Asia/Tokyo",
    "Asia/Almaty", "Asia/Tashkent", "Asia/Yekaterinburg", "Atlantic/Reykjavik",
    "Atlantic/Azores", "Atlantic/Cape_Verde", "Atlantic/Canary", "Australia/Perth",
    "Australia/Darwin", "Australia/Brisbane", "Australia/Adelaide", "Australia/Sydney",
    "Australia/Melbourne", "Australia/Hobart", "Europe/London", "Europe/Dublin",
    "Europe/Lisbon", "Europe/Madrid", "Europe/Paris", "Europe/Berlin", "Europe/Amsterdam",
    "Europe/Brussels", "Europe/Zurich", "Europe/Rome", "Europe/Vienna", "Europe/Prague",
    "Europe/Warsaw", "Europe/Stockholm", "Europe/Oslo", "Europe/Copenhagen",
    "Europe/Helsinki", "Europe/Athens", "Europe/Bucharest", "Europe/Istanbul",
    "Europe/Kyiv", "Europe/Moscow", "Indian/Maldives", "Indian/Mauritius",
    "Pacific/Honolulu", "Pacific/Auckland", "Pacific/Fiji", "Pacific/Guam",
    "Pacific/Port_Moresby", "Pacific/Tongatapu", "Pacific/Chatham",
)

# Append-only, like the rest — but a list of SERVICE NAMES rather than of
# selections, because the field over it is a BITMASK: bit i is SOURCE_CODES[i],
# and a selection is the OR of the services it names.
#
# A BITMASK RATHER THAN AN INDEX, and this is the one place in the file where the
# choice of representation is doing real work. A selection is a SET of services
# ("trakt", "simkl", "trakt+simkl"), so an index would need one entry per
# spelling — and the spellings of a set are combinatorial in the number of
# services, so registering a fourth would mean appending fifteen entries and
# every combination anybody had not thought to append would silently send its
# link out in the long form. One bit per service is linear instead: a new service
# is one name appended here, and every combination it takes part in is encodable
# the same day, without a decision.
#
# NAMES, NOT SELECTIONS, is also why the legacy "both" has no entry. It is a
# spelling of exactly {trakt, simkl} (app/sources/prefs.py says why it survives),
# so it packs as those two bits and comes back spelled as the set it always
# meant — which is what the Sources screen already rewrites it to on the next
# save. Nothing in the wire format has to remember an era.
#
# ZERO IS `auto`, which is the one selection that is not a set: it names no
# services, now or later. A named set always has at least one bit, so the two
# cannot be confused.
SOURCE_CODES = ("trakt", "simkl")

# The epoch the packed year+month counts from. Moving it would repoint every
# existing code at a different month, so it is as fixed as the codebooks.
_YM_EPOCH_YEAR = 1970

# version -> (bit, param name, hex width)... Order is both the bit order and the
# order the fields appear in the code, so a decoder walks it once. Add versions,
# never edit one: see rule 3.
_LAYOUTS: dict[str, tuple[tuple[int, str, int], ...]] = {
    "1": (
        (0x01, "endpoint", 1),
        (0x02, "card", 1),
        (0x04, "packing", 1),
        (0x08, "hidenw", 1),
        (0x10, "tz", 2),
        (0x20, "ym", 3),
        # APPENDED, which is the only reason this needed no new layout version.
        # A code written before this field existed cannot have 0x40 in its mask,
        # so a decoder skips it; and because the entry is last, every field ahead
        # of it sits at the character it always sat at. Both halves are pinned by
        # tests/calendar/test_share_code.py's PublishedCodeTests, against a string
        # off a real instance rather than against a round trip.
        #
        # TWO HEX FOR ONE BIT PER SERVICE, which is eight of them. The width is
        # what rule 3 is about, and the reason to buy the headroom now rather
        # than at the fourth registration is that widening it later is not a
        # widening at all — it is a new layout version and a second decoder path,
        # for a field one character short.
        (0x40, "source", 2),
    ),
}

_CODEBOOKS = {"endpoint": ENDPOINT_CODES, "card": CARD_CODES, "packing": PACKING_CODES,
              "tz": TZ_CODES}


def _pack_source(value: str) -> int | None:
    """A source selection as its bitmask, or None when this layout cannot say it.

    THE GRAMMAR IS ASKED FOR RATHER THAN REIMPLEMENTED. Which strings are
    selections, and which services a selection names, belong to
    app/sources/prefs.py; splitting on the separator here would be a second
    spelling of that grammar, in the one module whose answers are promises to
    links other people hold.

    A service with no bit yet — registered but never appended to SOURCE_CODES —
    yields None, so the link goes out as the long query string saying exactly
    what it means. That is rule 2 doing its job, and it is the failure the
    codebook growth test in tests/calendar/test_share_code.py exists to turn into
    a red suite rather than a silently longer URL.
    """
    text = str(value)
    if not source_prefs.is_selection(text):
        return None
    named = source_prefs.named_sources(text)
    if named is None:
        return 0  # `auto` — "whatever there is", which names nothing
    bits = 0
    for name in named:
        if name not in SOURCE_CODES:
            return None
        bits |= 1 << SOURCE_CODES.index(name)
    return bits


def _unpack_source(value: int) -> str | None:
    """A bitmask back into a selection, or None to drop the field entirely.

    A BIT THIS VERSION HAS NO SERVICE FOR DROPS THE WHOLE FIELD rather than
    decoding to the services it did recognize. A link naming three services
    read back as two would be a narrowing its author never asked for, applied
    silently — worse than the alternative, which is that the field goes missing
    and the page falls back to the owner's own preference and renders wider.
    Same instinct as an index naming nothing being dropped rather than guessed.
    """
    if value == 0:
        return source_prefs.AUTO
    if value >> len(SOURCE_CODES):
        return None
    names = [name for index, name in enumerate(SOURCE_CODES) if value & (1 << index)]
    return source_prefs.SEPARATOR.join(names)


def _index_of(field: str, value: str) -> int | None:
    try:
        return _CODEBOOKS[field].index(value)
    except ValueError:
        return None


def _pack_ym(year: str, month: str) -> int | None:
    try:
        y, m = int(year), int(month)
    except (TypeError, ValueError):
        return None
    if not (1 <= m <= 12) or y < _YM_EPOCH_YEAR:
        return None
    return (y - _YM_EPOCH_YEAR) * 12 + (m - 1)


def encode(params: dict) -> str | None:
    """`params` (the LINK_VIEW_PARAMS shape) as a `p=` code, or None when it
    cannot be represented — the caller then writes the long query string, which
    always works. Returns None for an empty view too: a link carrying no options
    at all should carry no query string either, not an empty code."""
    mask = 0
    parts: list[str] = []
    for bit, field, width in _LAYOUTS[VERSION]:
        if field == "ym":
            if "year" not in params or "month" not in params:
                continue
            value = _pack_ym(str(params["year"]), str(params["month"]))
        elif field == "hidenw":
            if "hidenw" not in params:
                continue
            value = {"0": 0, "1": 1}.get(str(params["hidenw"]))
        elif field == "source":
            if "source" not in params:
                continue
            value = _pack_source(str(params["source"]))
        else:
            if field not in params:
                continue
            value = _index_of(field, str(params[field]))
        # Unknown value, or one that has outgrown the field reserved for it.
        if value is None or value >= 16 ** width:
            return None
        mask |= bit
        parts.append(f"{value:0{width}X}")
    if not mask:
        return None
    return f"{VERSION}{mask:02X}" + "".join(parts)


def decode(code: str | None) -> dict:
    """A `p=` code back into params. Unreadable input — wrong version, truncated,
    non-hex, an index naming nothing — yields whatever was readable and drops the
    rest, never an exception: this parses a string a stranger can type."""
    text = (code or "").strip().upper()
    layout = _LAYOUTS.get(text[:1])
    if layout is None or len(text) < 3:
        return {}
    try:
        mask = int(text[1:3], 16)
    except ValueError:
        return {}

    out: dict[str, str] = {}
    pos = 3
    for bit, field, width in layout:
        if not mask & bit:
            continue
        chunk = text[pos:pos + width]
        pos += width
        if len(chunk) < width:
            break  # truncated: keep what came before it, stop reading
        try:
            value = int(chunk, 16)
        except ValueError:
            continue
        if field == "ym":
            out["year"] = str(_YM_EPOCH_YEAR + value // 12)
            out["month"] = str(value % 12 + 1)
        elif field == "hidenw":
            if value in (0, 1):
                out["hidenw"] = str(value)
        elif field == "source":
            if (selection := _unpack_source(value)) is not None:
                out["source"] = selection
        else:
            book = _CODEBOOKS[field]
            if value < len(book):
                out[field] = book[value]
    return out
