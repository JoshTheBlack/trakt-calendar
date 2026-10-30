"""Network logos: which logo a network has, and the badge drawn from it.

Trakt and Simkl give a show's network by NAME; TMDB gives per-network logos. Given
a show's tmdb id this asks TMDB for the show's networks, picks the one the name
means, and records its logo in `network_logos` — the network's TMDB id, the logo's
path, whether the logo is LIGHT, and when it was checked. A badge is then drawn
from that row at render:

  - THE PICTURE IS AN ADDRESS, never a file. The page draws TMDB's logo through
    the image proxy (providers/base.proxied_image) at the size a badge is, and CSS
    frames it. Nothing is written to disk and nothing is composited, so a badge is
    a table read for the whole page and a browser's own fetch of the picture.
  - THE FRAME FOLLOWS THE LOGO. A light logo on a light frame disappears, so a
    light one (mean luminance above `LIGHT_ABOVE`, measured on the very PNG the
    proxy hands the browser) is framed dark and everything else light. That one
    fact is stored with the row, so a page decides by reading it.
  - A NETWORK WITH NO LOGO IS A RECORDED ANSWER, not a missing one: TMDB was asked
    and named none, and the row says so (`missing`), so the badge is the 📡 and
    nothing asks again until the row is due.

ONLY THE DISCORD EMOJI DOWNLOAD COMPOSITES BYTES (`download_tile`): Discord wants
a 128px PNG with the logo on a rounded plate, which is a picture the proxy cannot
make, so that and nothing else uses Pillow — in a worker thread, because it is
synchronous.

TMDB-DERIVED, SO THE SIX-MONTH CEILING APPLIES to every word a row holds
(providers/tmdb/transport.MAX_CACHE_SECONDS). `fetched_at` is when TMDB last
answered, a row is rewritten whole when it is, and `sweep` deletes by it — and
`known` never draws a row past it even before a sweep has run. A row in use is
asked again long before that (`RECHECK_SECONDS`), so only a network nobody has
drawn for months ages out.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
from collections.abc import Iterable, Sequence
from enum import StrEnum
from io import BytesIO
from typing import NamedTuple

import anyio.to_thread
from PIL import Image, ImageDraw

from .. import db
from ..config import DATA_DIR
from ..perftrace import span
from ..providers.base import Media, proxied_image
from ..providers.tmdb import detail as tmdb_detail, transport as tmdb_transport

logger = logging.getLogger(__name__)

# What the proxy is asked for, for a badge: a square the logo is fitted inside
# (`contain`, never cropped or stretched) as a PNG, so a transparent logo stays
# transparent for the frame behind it. 128 is twice the largest badge CSS draws
# (26px), which is what a two-pixels-per-pixel screen needs and no more.
BADGE_PARAMS = "w=128&h=128&fit=contain&output=png"
# For the Discord download, which fits the logo inside a plate it composites on
# this side: asked larger so the plate is made from more than a badge's pixels.
SOURCE_PARAMS = "w=300&h=300&fit=contain&output=png"
# The mean luminance (0-255) above which a logo is drawn on a dark frame. The old
# tile used the same threshold for the same choice, so a logo keeps its look.
LIGHT_ABOVE = 150

# A network's logo is checked again after this long WHILE IT IS ON A PAGE, which
# renews the row's ceiling clock — a logo in use never ages out. A network TMDB
# had no logo for is asked sooner, because the likelier change is a title that
# TMDB now describes better.
RECHECK_SECONDS = 30 * 24 * 60 * 60
RECHECK_MISSING_SECONDS = 7 * 24 * 60 * 60
# After a failed ask (TMDB or the proxy could not be reached, or no title on the
# page was one TMDB knows) the next attempt waits this long, so a page that
# renders every few seconds does not retry every few seconds.
RETRY_SECONDS = 10 * 60
# How many shows on a network are tried, one after another, before it is recorded
# as having no logo — see `look_up`.
TITLES_TRIED = 3
# How many networks a warm resolves at once: each is two or three TMDB reads and a
# picture fetch, so eight keeps a warm well inside the TMDB pool's twenty and
# leaves room for the pages being served beside it.
BUILDS_AT_ONCE = 8

# The Discord emoji tile: a rounded plate, light logos on a dark one and dark
# logos on white, as the badge's frame is.
TILE = 128          # output square size (Discord emoji max)
PAD = 14            # transparent padding inside the tile
RADIUS = 26         # corner radius
_DARK_PLATE = (43, 45, 49, 255)
_WHITE_PLATE = (255, 255, 255, 255)

# The directory the old pipeline wrote its tiles and "no logo" markers to.
LEGACY_TILE_DIR = DATA_DIR / "logos"


class Logo(NamedTuple):
    """What a badge is drawn from: the picture's address through the image proxy,
    and whether it is a light logo (drawn on a dark frame)."""
    address: str
    light: bool


class Kind(StrEnum):
    FOUND = "found"
    MISSING = "missing"
    # TMDB, or the proxy, could not be asked, or nothing on the page was a title
    # TMDB knows: no answer about the NETWORK, so nothing is recorded as one.
    UNANSWERED = "unanswered"


class Resolution(NamedTuple):
    kind: Kind
    tmdb_network_id: int | None = None
    logo_path: str = ""
    light: bool = False


def address(logo_path: str, params: str = BADGE_PARAMS) -> str:
    """TMDB's logo at `logo_path`, through the image proxy.

    `original` for an SVG, which the CDN only serves at that size; the proxy
    rasterizes it, so no network needs a different path to a badge."""
    size = "original" if logo_path.lower().endswith(".svg") else "w300"
    return proxied_image(tmdb_transport.image_url(logo_path, size), params) or ""


def slug(network: str) -> str:
    """`network` as a file name for the download. A name with no Latin letter or
    digit in it is named by a digest of itself instead: stable, and its own, where
    the empty string left every such network with the same file name."""
    text = re.sub(r"[^a-z0-9]+", "-", (network or "").lower()).strip("-")
    if text or not network:
        return text or "network"
    return "n-" + hashlib.sha256(network.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# What a page reads
# ---------------------------------------------------------------------------

async def _rows(networks: Iterable[str]) -> dict[str, dict]:
    """The stored rows for these names, in ONE statement — a page of hundreds of
    badges is one read, never one per badge."""
    names = sorted({name for name in networks if name})
    if not names:
        return {}
    rows = await db.fetch_all(
        "SELECT network, logo_path, light, missing, checked_at, fetched_at, fail_count, "
        "last_failed_at FROM network_logos "
        "WHERE network IN (SELECT value FROM json_each(?))",
        (json.dumps(names),))
    return {row["network"]: dict(row) for row in rows}


def _drawable(row: dict, now: int) -> bool:
    """Whether a row has a logo that may still be drawn: one TMDB gave, inside the
    ceiling on every word it holds."""
    return (not row["missing"] and bool(row["logo_path"]) and row["fetched_at"] is not None
            and row["fetched_at"] > now - tmdb_transport.MAX_CACHE_SECONDS)


def _logos_of(rows: dict[str, dict], now: int) -> dict[str, Logo]:
    return {name: Logo(address(row["logo_path"]), bool(row["light"]))
            for name, row in rows.items() if _drawable(row, now)}


async def known(networks: Iterable[str]) -> dict[str, Logo]:
    """The badges these networks have, {name: Logo} — a network absent from the
    answer is drawn as the 📡 with its name.

    ASKED ONCE PER RESPONSE, SO A BADGE DOES NOT HAVE TO ASK THE BROWSER. A badge
    is the logo where there is one and the fallback where there is not, and that
    used to be settled by requesting the picture and handling the 404 in an
    `onerror` — a request per network with no logo, on every row, to learn
    something this table answers for nothing. Blank names are dropped rather than
    answered for: "" is not a network.
    """
    return _logos_of(await _rows(networks), db.now())


def is_due(row: dict | None, now: int) -> bool:
    """Whether a network should be asked about: never asked, or last answered long
    enough ago, or last failed long enough ago."""
    if row is None:
        return True
    if row["last_failed_at"] and now - row["last_failed_at"] < RETRY_SECONDS \
            and (row["checked_at"] is None or row["last_failed_at"] >= row["checked_at"]):
        return False
    if row["checked_at"] is None:
        return True
    wait = RECHECK_MISSING_SECONDS if row["missing"] else RECHECK_SECONDS
    return now - row["checked_at"] >= wait


async def for_page(settings, roster: Iterable[tuple[str | None, object]]) -> dict[str, Logo]:
    """`known` for a signed-in page, and the warm-up of what it lacks.

    `roster` is (network name, a tmdb id of some title on it), one pair per title.
    The page draws the badges that exist; the rest are resolved BEHIND the response
    (`warm_later`), so a new network's logo appears on the next load rather than
    delaying this one — resolving inline cost a render seven seconds cold, because a
    network nobody has resolved means a TMDB lookup and one month can name a
    hundred of them.
    """
    titles: dict[str, list] = {}
    for name, tmdb in roster:
        if not name:
            continue
        ids = titles.setdefault(name, [])
        if tmdb and tmdb not in ids and len(ids) < TITLES_TRIED:
            ids.append(tmdb)
    rows = await _rows(titles)
    now = db.now()
    due = {name: tuple(ids) for name, ids in titles.items()
           if ids and is_due(rows.get(name), now)}
    if due:
        warm_later(settings, due)
    return _logos_of(rows, now)


# ---------------------------------------------------------------------------
# Resolving a network's logo
# ---------------------------------------------------------------------------

def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def _pick_network(networks: list[dict], want: str) -> dict | None:
    """Choose the TMDB network a network name means from a show's networks. Exact
    normalized match first; then a prefix/containment match so combined brands line
    up (Trakt "Paramount+ with Showtime" -> TMDB "Paramount+", "AMC+" -> "AMC").

    NONE WHEN NO NETWORK OF THE SHOW IS THE ONE MEANT, which is a show that is
    simply not evidence about this network: TMDB files it under another one (a show
    Trakt calls BBC Two that TMDB lists on Channel 4). This used to fall back to
    the show's first network, and a badge drawn from it was a WRONG logo — measured
    on a copy of the owner's data, "BBC Two" drew Channel 4's and "Gizmoplex"
    Comedy Central's — where a 📡 with the right name is the honest answer. Another
    show on the same network is tried instead (`look_up`)."""
    want_n = _norm(want)
    if not networks:
        return None
    exact = next((n for n in networks if _norm(n.get("name")) == want_n), None)
    if exact:
        return exact
    for n in networks:
        nn = _norm(n.get("name"))
        if nn and len(nn) >= 3 and (want_n.startswith(nn) or nn.startswith(want_n)
                                    or nn in want_n or want_n in nn):
            logger.info("network match: %r ~= TMDB %r (fuzzy)", want, n.get("name"))
            return n
    return None


async def _network_image_path(settings, network_id) -> str:
    """A logo file_path from /network/{id}/images, for a network whose entry on the
    show carries none — a raster one where there is one, any otherwise (the proxy
    rasterizes an SVG). "" when TMDB lists none."""
    if not network_id:
        return ""
    try:
        data = await tmdb_transport.cached_get(settings, f"network/{network_id}/images")
    except tmdb_transport.TmdbNotFound:
        return ""
    paths = [logo.get("file_path") or "" for logo in (data or {}).get("logos") or []]
    paths = [path for path in paths if path]
    return next((path for path in paths if not path.lower().endswith(".svg")),
                paths[0] if paths else "")


def _luminance(img: Image.Image) -> float:
    """The mean luminance (0-255) of a picture's opaque pixels."""
    small = img.convert("RGBA").resize((32, 32))
    px = small.load()
    total, n = 0.0, 0
    for y in range(32):
        for x in range(32):
            r, g, b, a = px[x, y]
            if a > 40:
                total += 0.2126 * r + 0.7152 * g + 0.0722 * b
                n += 1
    return (total / n) if n else 0.0


def mean_luminance(raw: bytes) -> float | None:
    """The mean luminance (0-255) of an image's opaque pixels, or None for bytes
    that are not a picture. Synchronous (Pillow): callers run it in a worker."""
    try:
        return _luminance(Image.open(BytesIO(raw)))
    except Exception as exc:  # Pillow raises a family of types for a bad file
        logger.warning("Pillow could not open a logo (%d bytes): %s", len(raw or b""), exc)
        return None


async def _is_light(logo_path: str) -> bool | None:
    """Whether the logo, as the proxy will hand it to a browser, is light — None
    when the picture could not be fetched or is not one (so nothing is decided
    from it)."""
    raw = await tmdb_transport.download(address(logo_path))
    if raw is None:
        return None
    lum = await anyio.to_thread.run_sync(mean_luminance, raw)
    return None if lum is None else lum > LIGHT_ABOVE


async def _look_up_through(settings, network: str, tmdb_id) -> Resolution:
    """What one show says about `network`'s logo: FOUND, MISSING (TMDB described the
    show and it names no usable logo for this network) or UNANSWERED (it could not
    be asked, the show is not one TMDB knows, or the picture could not be judged)."""
    try:
        data = await tmdb_detail.title_payload(settings, tmdb_id, Media.SHOW)
        if not data:
            logger.info("logo[%s]: tmdb %s is not a title TMDB knows", network, tmdb_id)
            return Resolution(Kind.UNANSWERED)
        chosen = _pick_network(data.get("networks") or [], network)
        if chosen is None:
            return Resolution(Kind.MISSING)
        logo_path = chosen.get("logo_path") or ""
        network_id = chosen.get("id")
        if not logo_path:
            logo_path = await _network_image_path(settings, network_id)
    except tmdb_transport.TmdbError as exc:
        logger.warning("logo[%s]: TMDB could not be asked (%s)", network, exc)
        return Resolution(Kind.UNANSWERED)
    if not logo_path:
        return Resolution(Kind.MISSING, network_id)
    light = await _is_light(logo_path)
    if light is None:
        return Resolution(Kind.UNANSWERED)
    logger.info("logo[%s]: network=%r id=%s logo_path=%s light=%s",
                network, chosen.get("name"), network_id, logo_path, light)
    return Resolution(Kind.FOUND, network_id, logo_path, light)


async def look_up(settings, network: str, tmdb_ids: Sequence) -> Resolution:
    """Resolve `network`'s logo through shows `tmdb_ids`, which are on it, until
    one of them says.

    FOUND, MISSING OR UNANSWERED, and the last is not a verdict. A TMDB that could
    not be asked, a proxy that could not be reached and a title TMDB does not know
    say nothing about the NETWORK — recording any of them as "no logo" would hide
    a logo until the row aged out. MISSING is TMDB having described at least one
    show on the network and having nothing usable for it: no network of the show
    is the one meant, no logo on it or in its image list.

    SEVERAL SHOWS, because one show can be filed by TMDB under another network than
    the one Trakt names it by, and that show alone would have called a network with a
    logo one with none. The first show that names the network decides.
    """
    with span("logo.look_up", network=network, tmdb=list(tmdb_ids)):
        answered = False
        for tmdb_id in tmdb_ids:
            resolution = await _look_up_through(settings, network, tmdb_id)
            if resolution.kind is Kind.FOUND:
                return resolution
            answered = answered or resolution.kind is Kind.MISSING
        return Resolution(Kind.MISSING if answered else Kind.UNANSWERED)


async def record(network: str, resolution: Resolution) -> None:
    """Write what a look-up found. EVERY WORD OF THE ROW AT ONCE, so `fetched_at`
    is the age of all of them (the ceiling is about every word and a row has one
    clock). An unanswered look-up writes only the failure, and leaves a logo the
    row already holds exactly as it was."""
    now = db.now()
    if resolution.kind is Kind.UNANSWERED:
        await db.execute(
            "INSERT INTO network_logos (network, fail_count, last_failed_at) VALUES (?, 1, ?) "
            "ON CONFLICT(network) DO UPDATE SET fail_count = fail_count + 1, "
            "last_failed_at = excluded.last_failed_at",
            (network, now))
        return
    missing = resolution.kind is Kind.MISSING
    await db.execute(
        "INSERT INTO network_logos (network, tmdb_network_id, logo_path, light, missing, "
        "checked_at, fetched_at, fail_count, last_failed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL) "
        "ON CONFLICT(network) DO UPDATE SET tmdb_network_id = excluded.tmdb_network_id, "
        "logo_path = excluded.logo_path, light = excluded.light, missing = excluded.missing, "
        "checked_at = excluded.checked_at, fetched_at = excluded.fetched_at, "
        "fail_count = 0, last_failed_at = NULL",
        (network, resolution.tmdb_network_id, resolution.logo_path, int(resolution.light),
         int(missing), now, now))


async def warm(settings, roster: dict[str, Sequence]) -> int:
    """Resolve the networks in `roster` ({name: tmdb ids of shows on it}), at most
    `BUILDS_AT_ONCE` at a time. Returns how many came out with a logo.

    UNBOUNDED, A MONTH'S WARM STARTED EVERY MISSING NETWORK AT ONCE — measured,
    362 of them, ~700 requests queued on a 20-connection pool, one logo waiting up
    to 12 s and everything else on the instance waiting behind the backlog.
    """
    if not getattr(settings, "tmdb_configured", False):
        return 0
    gate = asyncio.Semaphore(BUILDS_AT_ONCE)

    async def one(name: str, tmdb_ids) -> Resolution:
        async with gate:
            resolution = await look_up(settings, name, tmdb_ids)
        await record(name, resolution)
        return resolution

    with span("logos.warm", n=len(roster)):
        results = await asyncio.gather(
            *(one(name, ids) for name, ids in roster.items()), return_exceptions=True)
    found = 0
    for name, result in zip(roster, results):
        if isinstance(result, BaseException):
            logger.warning("logo warm-up failed for %s: %s", name, result)
        elif result.kind is Kind.FOUND:
            found += 1
    return found


# AT MOST ONE WARM-UP IN FLIGHT FOR THE WHOLE PROCESS. Every page render can find
# networks that are due, and a busy instance would otherwise start a task per
# request, all resolving the same networks against the same rate-limited API. One
# at a time is enough: the work is idempotent and whatever this pass misses the
# next render asks for again. The task is held here so it cannot be collected
# mid-run; it is one object, replaced by the next warm-up, and cleared when done.
_warming: asyncio.Task | None = None


def warm_later(settings, roster: dict[str, Sequence]) -> None:
    """Resolve what `roster` lacks off the back of the request that found it."""
    global _warming
    if _warming is not None:
        return

    async def run() -> None:
        global _warming
        try:
            await warm(settings, roster)
        except Exception:
            # Best-effort by design: a network whose logo cannot be resolved shows
            # the 📡, which is exactly what it showed before. Never let it reach
            # the request that scheduled it — that request has long since been
            # answered.
            logger.exception("network logo warm-up failed")
        finally:
            _warming = None

    _warming = asyncio.create_task(run())


class Recheck(StrEnum):
    FOUND = "found"
    KEPT = "kept"
    MISSING = "missing"
    UNANSWERED = "unanswered"


async def recheck(settings, network: str, tmdb_ids: Sequence) -> Recheck:
    """Resolve `network` again now, whether or not it is due (the administrator's
    ↻), and say what came of it.

    THE OLD LOGO IS KEPT UNTIL A NEW ONE EXISTS. A recheck that comes out with no
    logo while the row holds one changes nothing and says so (`KEPT`), where
    recording it would have turned a network with a logo into one with none; and a
    TMDB that could not be asked changes nothing at all.
    """
    resolution = await look_up(settings, network, tmdb_ids)
    held = (await _rows([network])).get(network)
    if resolution.kind is Kind.UNANSWERED:
        return Recheck.UNANSWERED
    if resolution.kind is Kind.MISSING and held is not None and _drawable(held, db.now()):
        return Recheck.KEPT
    await record(network, resolution)
    return Recheck(resolution.kind.value)


# ---------------------------------------------------------------------------
# The Discord emoji download
# ---------------------------------------------------------------------------

def render_tile(raw: bytes) -> bytes | None:
    """Trim -> fit -> a rounded plate that suits the logo -> centred PNG tile, as
    PNG bytes; None for bytes that are not a picture. The one place this module
    composites, and synchronous: callers run it in a worker thread."""
    with span("logo.render", src_bytes=len(raw or b"")) as sp:
        try:
            src = Image.open(BytesIO(raw)).convert("RGBA")
        except Exception as exc:  # Pillow raises a family of types for a bad file
            logger.warning("Pillow could not open logo (%d bytes): %s", len(raw or b""), exc)
            return None
        bbox = src.getbbox()
        if bbox:
            src = src.crop(bbox)
        inner = TILE - 2 * PAD
        src.thumbnail((inner, inner), Image.LANCZOS)
        light = _luminance(src) > LIGHT_ABOVE
        plate = _DARK_PLATE if light else _WHITE_PLATE
        mask = Image.new("L", (TILE, TILE), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, TILE - 1, TILE - 1], radius=RADIUS, fill=255)
        tile = Image.composite(
            Image.new("RGBA", (TILE, TILE), plate),
            Image.new("RGBA", (TILE, TILE), (0, 0, 0, 0)),
            mask)
        tile.alpha_composite(src, ((TILE - src.width) // 2, (TILE - src.height) // 2))
        sp.set(fit=f"{src.width}x{src.height}", plate="dark" if light else "white")
        out = BytesIO()
        tile.save(out, format="PNG")
        return out.getvalue()


async def download_tile(network: str) -> bytes | None:
    """The Discord emoji tile for `network` as PNG bytes, or None when the network
    has no logo to make one from (or the picture could not be fetched).

    MADE WHEN ASKED AND KEPT NOWHERE: it is a download somebody presses, not a
    thing a page draws, so there is no file to cache, age or sweep."""
    row = (await _rows([network])).get(network)
    if row is None or not _drawable(row, db.now()):
        return None
    raw = await tmdb_transport.download(address(row["logo_path"], SOURCE_PARAMS))
    if raw is None:
        return None
    return await anyio.to_thread.run_sync(render_tile, raw)


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------

async def sweep(now: int | None = None) -> int:
    """Delete network rows past the ceiling. Returns how many went.

    BY `fetched_at`, THE AGE OF TMDB'S LAST ANSWER, so a row that was only ever a
    failure (no answer yet) is not this sweep's: it holds nothing from TMDB. A
    "no logo" row ages out too — keeping one past the ceiling would make one
    absent answer permanent, which is what the ceiling exists to prevent."""
    cutoff = (db.now() if now is None else now) - tmdb_transport.MAX_CACHE_SECONDS
    cursor = await db.execute(
        "DELETE FROM network_logos WHERE fetched_at IS NOT NULL AND fetched_at <= ?",
        (cutoff,))
    return cursor.rowcount


def discard_legacy_tiles() -> None:
    """Remove the old pipeline's tiles and "no logo" markers (data/logos). Called
    at startup; idempotent — the directory is normally already gone on later runs.

    NOTHING READS IT ANY MORE. A badge is drawn from `network_logos` through the
    image proxy, so the files are dead weight; deleting them at once rather than
    letting an age sweep reach them frees the disk now and leaves no code whose only
    job is to age out a directory nothing writes."""
    if not LEGACY_TILE_DIR.exists():
        return
    try:
        shutil.rmtree(LEGACY_TILE_DIR)
        logger.info("Removed the legacy network logo tiles at %s (logos are drawn "
                    "through the image proxy now)", LEGACY_TILE_DIR)
    except OSError:
        logger.warning("Could not remove the legacy logo directory %s", LEGACY_TILE_DIR)
