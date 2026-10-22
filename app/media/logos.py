"""Network logo tiles for the calendar cards + distrakt (TMDB + Pillow).

Trakt gives only a network NAME; TMDB gives per-network logos. Given a show's
tmdb_id we fetch /tv/{id} -> networks[].logo_path, download the logo, and process
it with Pillow into a small rounded tile (adaptive background so light-on-transparent
logos stay visible), cached on disk keyed by the (Trakt) network name. Both the
calendar and distrakt read the same cache via GET /api/network-logo.

Cache: data/logos/<slug>.png (tile) and data/logos/<slug>.none (negative marker,
so a network with no usable logo — SVG-only / no match / no key — isn't re-fetched
every request). Delete the files to regenerate.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import re
from io import BytesIO
from pathlib import Path

import anyio.to_thread
from PIL import Image, ImageDraw

from ..config import DATA_DIR
from ..providers.base import Media
from ..providers.tmdb import detail as tmdb_detail, transport as tmdb_transport
from ..perftrace import span

logger = logging.getLogger(__name__)
_perf = logging.getLogger("app.perf")

LOGO_DIR = DATA_DIR / "logos"
TMDB_IMG = tmdb_transport.IMAGE_BASE

TILE = 128          # output square size (Discord emoji max)
# How many network logos a warm builds at once: each is two or three TMDB reads
# and an image download, so eight keeps a warm well inside the TMDB pool's twenty
# and leaves room for the pages being served beside it. posters.py's fan-out is
# the same eight, for the same reason.
_BUILDS_AT_ONCE = 8
PAD = 14            # transparent padding inside the tile
RADIUS = 26         # corner radius
_LIGHT_BG = (43, 45, 49, 255)      # for light/white logos
_WHITE_BG = (255, 255, 255, 255)   # for dark/colored logos


def slug(network: str) -> str:
    """`network` as a file name. A name with no Latin letter or digit in it —
    哔哩哔哩, НТВ — slugs to nothing, and every such network used to share the
    one file ".png" and the one marker ".none", so a single one with no logo
    hid all the others and none could be written at all ("unknown file
    extension"). Such a name is named by a digest of itself instead: stable,
    and its own."""
    text = re.sub(r"[^a-z0-9]+", "-", (network or "").lower()).strip("-")
    if text or not network:
        return text
    return "n-" + hashlib.sha256(network.encode("utf-8")).hexdigest()[:16]


def _tile_path(network: str) -> Path:
    return LOGO_DIR / f"{slug(network)}.png"


def _none_path(network: str) -> Path:
    return LOGO_DIR / f"{slug(network)}.none"


def cached_tile(network: str) -> Path | None:
    """The on-disk tile for `network` if already generated, else None."""
    p = _tile_path(network)
    return p if (network and p.exists()) else None


def tiled(networks) -> set[str]:
    """Which of `networks` already have a tile on disk.

    ASKED ONCE PER RESPONSE SO A ROW DOES NOT HAVE TO ASK THE BROWSER. A network
    badge is the tile if there is one and the viewer's own emoji if there is not,
    and that used to be settled by requesting the tile and handling the 404 in an
    `onerror` — a request per network with no logo, on every row, to learn
    something the filesystem answers here for nothing. The tracker warms every
    network on the page before it renders (`ensure_logos`), so by render time the
    answer is final.

    Blank names are dropped rather than answered for: "" is not a network.
    """
    return {name for name in {n for n in networks if n} if cached_tile(name)}


def versions(networks) -> dict[str, int]:
    """Which of `networks` have a tile on disk, each with WHEN it was written.

    `tiled` with a version beside each answer, for a page that puts the version
    into the tile's address. The tile route answers with a day's private cache
    lifetime, so an address that stayed the same across a regenerate would go on
    showing the browser's copy of the old picture; with the version in it, a new
    tile is a new address and the old copy is never asked for again.
    """
    found = {}
    for name in {n for n in networks if n}:
        path = cached_tile(name)
        if path is None:
            continue
        try:
            found[name] = path.stat().st_mtime_ns
        except OSError:
            continue  # swept between the check and the stat: no tile after all
    return found


def is_negative(network: str) -> bool:
    return bool(network) and _none_path(network).exists()


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def _pick_network(networks: list[dict], want: str) -> dict | None:
    """Choose the TMDB network for a Trakt network name. Exact normalized match
    first; then a prefix/containment match so combined brands line up (Trakt
    "Paramount+ with Showtime" -> TMDB "Paramount+", "AMC+" -> "AMC"). Falls back
    to the first network so /network/{id}/images can still be tried."""
    want_n = _norm(want)
    if not networks:
        return None
    exact = next((n for n in networks if _norm(n.get("name")) == want_n), None)
    if exact:
        return exact
    for n in networks:
        nn = _norm(n.get("name"))
        if nn and len(nn) >= 3 and (want_n.startswith(nn) or nn.startswith(want_n) or nn in want_n or want_n in nn):
            logger.info("network match: Trakt %r ~= TMDB %r (fuzzy)", want, n.get("name"))
            return n
    return networks[0]


async def _tmdb_tv_networks(settings, tmdb_id) -> list[dict]:
    """The show's networks, from the same cached `/tv/{id}` answer TMDB's
    details panel reads — no second request for a show already described. []
    for a show TMDB does not know; raises TmdbError when TMDB could not be asked."""
    data = await tmdb_detail.title_payload(settings, tmdb_id, Media.SHOW)
    nets = (data or {}).get("networks") or []
    logger.info("TMDB tv/%s -> %d network(s): %s", tmdb_id, len(nets), [n.get("name") for n in nets])
    return nets


async def _network_raster_logo(settings, network_id) -> str | None:
    """A non-SVG logo file_path from /network/{id}/images (a raster alternative to
    a network whose primary /tv logo is an SVG)."""
    if not network_id:
        return None
    try:
        data = await tmdb_transport.cached_get(settings, f"network/{network_id}/images")
    except tmdb_transport.TmdbNotFound:
        return None
    for lg in (data or {}).get("logos") or []:
        fp = lg.get("file_path") or ""
        if fp and not fp.lower().endswith(".svg"):
            logger.info("network %s: raster logo alternative %s", network_id, fp)
            return fp
    return None


def _rasterize_svg(svg_bytes: bytes) -> bytes | None:
    """SVG -> PNG bytes via cairosvg. cairosvg needs the native Cairo library
    (present on Linux/Docker; commonly missing on Windows) — degrade to None if
    it can't load, so the caller falls back to the emoji."""
    try:
        import cairosvg
    except Exception as exc:  # ImportError, or OSError when libcairo is missing
        logger.info("cairosvg unavailable (%s) — SVG-only logo skipped", exc)
        return None
    try:
        with span("logo.svg_rasterize", bytes=len(svg_bytes or b"")):
            return cairosvg.svg2png(bytestring=svg_bytes, output_width=256, output_height=256)
    except Exception as exc:
        logger.warning("cairosvg rasterize failed: %s", exc)
        return None


def _avg_luminance(img: Image.Image) -> float:
    """Mean luminance (0-255) of the logo's opaque pixels."""
    small = img.resize((32, 32))
    px = small.load()
    total, n = 0.0, 0
    for y in range(32):
        for x in range(32):
            r, g, b, a = px[x, y]
            if a > 40:
                total += 0.2126 * r + 0.7152 * g + 0.0722 * b
                n += 1
    return (total / n) if n else 0.0


def _render_tile(raw: bytes) -> Image.Image | None:
    """Trim -> fit -> adaptive rounded background -> centered PNG tile."""
    with span("logo.render", src_bytes=len(raw or b"")) as sp:
        try:
            src = Image.open(BytesIO(raw)).convert("RGBA")
        except Exception as exc:
            logger.warning("Pillow could not open logo (%d bytes): %s", len(raw or b""), exc)
            return None
        orig = src.size
        bbox = src.getbbox()
        if bbox:
            src = src.crop(bbox)
        inner = TILE - 2 * PAD
        src.thumbnail((inner, inner), Image.LANCZOS)

        lum = _avg_luminance(src)
        bg = _LIGHT_BG if lum > 150 else _WHITE_BG
        mask = Image.new("L", (TILE, TILE), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, TILE - 1, TILE - 1], radius=RADIUS, fill=255)
        tile = Image.composite(
            Image.new("RGBA", (TILE, TILE), bg),
            Image.new("RGBA", (TILE, TILE), (0, 0, 0, 0)),
            mask,
        )
        tile.alpha_composite(src, ((TILE - src.width) // 2, (TILE - src.height) // 2))
        sp.set(orig=f"{orig[0]}x{orig[1]}", fit=f"{src.width}x{src.height}",
               lum=f"{lum:.0f}", bg="dark" if lum > 150 else "white")
    return tile


async def ensure_logo(settings, network: str, tmdb_id) -> Path | None:
    """Return the cached tile for `network`, generating it from `tmdb_id` if
    needed. None (and a negative marker) when there's no usable logo."""
    if not network:
        return None
    tile = _tile_path(network)
    if tile.exists():
        return tile
    if is_negative(network):
        logger.info("logo[%s]: negative-cached (skipping TMDB)", network)
        return None
    if not getattr(settings, "tmdb_configured", False) or not tmdb_id:
        logger.info("logo[%s]: cannot generate (tmdb_configured=%s, tmdb_id=%s)",
                    network, getattr(settings, "tmdb_configured", False), tmdb_id)
        return None

    try:
        return await _generate_once(settings, network, tmdb_id, tile)
    except tmdb_transport.TmdbError as exc:
        # A TMDB THAT COULD NOT BE ASKED IS NOT A NETWORK WITH NO LOGO, so no
        # negative marker is written: the next request asks again, where a
        # marker would have hidden the logo until it aged out.
        logger.warning("logo[%s]: TMDB could not be asked (%s)", network, exc)
        return None


# The tiles being built, by file name, so a page's warm and a row's own request
# for the same network share one build rather than fetching and writing it twice.
# In process: one worker is what this app runs. The same shape as
# posters._look_up_once.
_BUILDING: dict[str, asyncio.Task] = {}


async def _generate_once(settings, network: str, tmdb_id, tile: Path) -> Path | None:
    """`_generate`, joined by anyone asking for the same network while it runs."""
    key = slug(network)
    task = _BUILDING.get(key)
    if task is None:
        task = asyncio.ensure_future(_generate(settings, network, tmdb_id, tile))
        _BUILDING[key] = task
        task.add_done_callback(lambda _done: _BUILDING.pop(key, None))
    return await asyncio.shield(task)


def _write_tile(raw: bytes | None, svg: bytes | None, tile: Path, marker: Path) -> bool:
    """The CPU and disk half of a build, run in a worker thread: rasterize an SVG
    if that is all there is, render the tile, and write it — or write the
    negative marker. True when a tile was written.

    OFF THE EVENT LOOP because Pillow and cairosvg are synchronous, and a failed
    cairosvg import is retried (and searches for its native library) on every
    call where it is missing. Measured on an empty logo cache, the whole build's
    synchronous work came to ~1 ms a tile — but a month's warm builds hundreds,
    and every millisecond of it on the loop is a millisecond every other request
    waits."""
    if raw is None and svg is not None:
        raw = _rasterize_svg(svg)
    img = _render_tile(raw) if raw else None
    if img is None:
        marker.write_text("", encoding="utf-8")
        return False
    img.save(tile, format="PNG")
    return True


async def _generate(settings, network: str, tmdb_id, tile: Path) -> Path | None:
    """`ensure_logo`'s TMDB half: look the logo up, render it, and either store
    the tile or record that there is none."""
    with span("logo.generate", network=network, tmdb=tmdb_id):
        LOGO_DIR.mkdir(parents=True, exist_ok=True)
        networks = await _tmdb_tv_networks(settings, tmdb_id)
        chosen = _pick_network(networks, network) or {}
        logo_path = chosen.get("logo_path")
        network_id = chosen.get("id")
        logger.info("logo[%s]: chosen network=%r id=%s logo_path=%s",
                    network, chosen.get("name"), network_id, logo_path)

        raw = svg = None
        if logo_path and not logo_path.lower().endswith(".svg"):
            raw = await tmdb_transport.download(f"{TMDB_IMG}/w300{logo_path}")
        else:
            # Primary logo is SVG (or missing): prefer a raster alternative from the
            # network's image list; only if there's none do we rasterize the SVG.
            alt = await _network_raster_logo(settings, network_id)
            if alt:
                raw = await tmdb_transport.download(f"{TMDB_IMG}/w300{alt}")
            elif logo_path:
                svg = await tmdb_transport.download(f"{TMDB_IMG}/original{logo_path}")

        made = await anyio.to_thread.run_sync(_write_tile, raw, svg, tile, _none_path(network))
        if not made:
            logger.info("logo[%s]: no usable raster logo -> negative marker", network)
            return None
        logger.info("logo[%s]: GENERATED -> %s", network, tile.name)
    return tile


async def regenerate(settings, network: str, tmdb_id) -> Path | None:
    """Make `network`'s tile again from TMDB, and return it — or None, with the
    tile it had left exactly where it was.

    THE OLD TILE IS SET ASIDE, NOT DELETED, until a new one exists. Dropping the
    tile first and re-resolving — which is what this was — turned a regenerate
    that made nothing into a network with NO logo. `ensure_logo` now tells an
    unreachable TMDB apart from a missing logo and marks only the second, but a
    regenerate that makes nothing should still change nothing.
    """
    tile = _tile_path(network)
    previous = tile.with_suffix(".previous")
    had = tile.exists()
    if had:
        tile.replace(previous)
    _none_path(network).unlink(missing_ok=True)

    def put_back() -> None:
        _none_path(network).unlink(missing_ok=True)
        if had:
            previous.replace(tile)

    try:
        made = await ensure_logo(settings, network, tmdb_id)
    except BaseException:
        put_back()
        raise
    if made is None:
        put_back()
        return None
    previous.unlink(missing_ok=True)
    return made


async def ensure_logos(settings, roster) -> int:
    """Best-effort pre-generation of the network tiles a roster needs. Returns how
    many were newly generated.

    `roster` is any iterable of (network_name, tmdb_id). WHY THIS EXISTS: both the
    calendar and the tracker render each network as an <img> pointing at
    /api/network-logo, which generates the tile on a cache miss — but ONLY when
    that request carries a usable tmdb. A show added before tmdb was stored sends
    an empty one, so if a network appears solely on such shows its tile is never
    generated and it shows the emoji fallback forever, even though a sibling with
    a tmdb could have produced it. Warming the cache from the whole roster removes
    that dependence on which show happened to load first.

    Cheap once warm: a network whose tile already exists (or is negative-cached)
    is a single path check and no TMDB call, so this is safe to run on every load
    — it does real work only for the genuinely-missing, exactly like backfill.
    """
    if not getattr(settings, "tmdb_configured", False):
        return 0
    # First tmdb seen per still-missing network; dedup so one TMDB lookup covers
    # every show on that network.
    want: dict[str, int] = {}
    for network, tmdb in roster:
        name = (network or "").strip()
        if not name or not tmdb or name in want:
            continue
        if _tile_path(name).exists() or is_negative(name):
            continue
        want[name] = int(tmdb)
    if not want:
        return 0
    # AT MOST _BUILDS_AT_ONCE AT A TIME. Unbounded, a month's warm started every
    # missing network at once — measured, 362 of them, ~700 requests queued on a
    # 20-connection pool, one logo waiting up to 12 s and everything else on the
    # instance waiting behind the backlog.
    gate = asyncio.Semaphore(_BUILDS_AT_ONCE)

    async def _one(name, tmdb):
        async with gate:
            return await ensure_logo(settings, name, tmdb)

    with span("logos.ensure_logos", n=len(want)):
        results = await asyncio.gather(
            *(_one(name, tmdb) for name, tmdb in want.items()),
            return_exceptions=True,
        )
    generated = 0
    for name, result in zip(want, results):
        if isinstance(result, Exception):
            logger.warning("logo pre-warm failed for %s: %s", name, result)
        elif result is not None:
            generated += 1
    return generated


def sweep(now: float | None = None) -> int:
    """Reclaim network-logo tiles past their age ceiling. Returns how many went.

    AGE ONLY, WITH NO SIZE CAP, and the asymmetry with the poster cache beside
    it is deliberate rather than an omission. These are TMDB-sourced, so
    tmdb_transport.MAX_CACHE_SECONDS (providers/tmdb/transport.py) applies to them exactly as it applies to posters —
    but there are as many of them as there are networks, measured at a few
    megabytes in total, so a byte budget would be a setting nobody could ever
    have a reason to change.

    NEGATIVE MARKERS AGE OUT TOO. A marker records that TMDB had no logo for a
    network at the time; keeping it past the ceiling would make one absent
    answer permanent, which is the same thing the ceiling exists to prevent for
    a picture that IS there.

    Pure filesystem walking, so the caller runs it through a worker thread — see
    app/main.py, and posters.sweep beside it for the same reason.
    """
    if not LOGO_DIR.exists():
        return 0
    cutoff = (time.time() if now is None else now) - tmdb_transport.MAX_CACHE_SECONDS
    removed = 0
    for path in LOGO_DIR.rglob("*"):
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime > cutoff:
                continue
            path.unlink()
        except OSError:
            continue
        removed += 1
    return removed
