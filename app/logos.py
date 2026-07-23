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
import logging
import re
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

from .config import DATA_DIR
from .perftrace import span

logger = logging.getLogger(__name__)
_perf = logging.getLogger("app.perf")

LOGO_DIR = DATA_DIR / "logos"
TMDB_API = "https://api.themoviedb.org/3"
TMDB_IMG = "https://image.tmdb.org/t/p"

TILE = 128          # output square size (Discord emoji max)
PAD = 14            # transparent padding inside the tile
RADIUS = 26         # corner radius
_LIGHT_BG = (43, 45, 49, 255)      # for light/white logos
_WHITE_BG = (255, 255, 255, 255)   # for dark/colored logos


def _slug(network: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (network or "").lower()).strip("-")


def _tile_path(network: str) -> Path:
    return LOGO_DIR / f"{_slug(network)}.png"


def _none_path(network: str) -> Path:
    return LOGO_DIR / f"{_slug(network)}.none"


def cached_tile(network: str) -> Path | None:
    """The on-disk tile for `network` if already generated, else None."""
    p = _tile_path(network)
    return p if (network and p.exists()) else None


def is_negative(network: str) -> bool:
    return bool(network) and _none_path(network).exists()


def delete(network: str) -> None:
    """Drop a network's cached tile + negative marker so it re-resolves next time."""
    for p in (_tile_path(network), _none_path(network)):
        try:
            p.unlink()
        except OSError:
            pass


def _is_v4_token(key: str) -> bool:
    # TMDB v4 read tokens are long JWTs ("eyJ..."); v3 keys are short hex.
    return key.startswith("eyJ") or len(key) > 60


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


async def _tmdb_get(settings, path: str, label: str) -> dict | None:
    """GET a TMDB API path (auth via v4 bearer or v3 api_key). Returns parsed JSON."""
    from .trakt import shared_client
    key = (settings.tmdb_api_key or "").strip()
    headers, params = {}, {}
    if _is_v4_token(key):
        headers["Authorization"] = f"Bearer {key}"
    else:
        params["api_key"] = key
    auth = "v4/bearer" if _is_v4_token(key) else "v3/api_key"
    with span(label, path=path, auth=auth) as sp:
        try:
            resp = await shared_client().get(f"{TMDB_API}{path}", params=params, headers=headers)
        except Exception as exc:  # network / client error
            logger.warning("TMDB %s failed: %s", path, exc)
            return None
        sp.set(status=resp.status_code)
    if resp.status_code != 200:
        logger.warning("TMDB %s -> HTTP %s: %s", path, resp.status_code, resp.text[:160])
        return None
    try:
        return resp.json()
    except ValueError:
        return None


async def _tmdb_tv_networks(settings, tmdb_id) -> list[dict]:
    data = await _tmdb_get(settings, f"/tv/{tmdb_id}", "logo.tmdb_tv")
    nets = (data or {}).get("networks") or []
    logger.info("TMDB tv/%s -> %d network(s): %s", tmdb_id, len(nets), [n.get("name") for n in nets])
    return nets


async def _network_raster_logo(settings, network_id) -> str | None:
    """A non-SVG logo file_path from /network/{id}/images (a raster alternative to
    a network whose primary /tv logo is an SVG)."""
    if not network_id:
        return None
    data = await _tmdb_get(settings, f"/network/{network_id}/images", "logo.network_images")
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


async def _download(url: str) -> bytes | None:
    from .trakt import shared_client
    with span("logo.download") as sp:
        try:
            resp = await shared_client().get(url)
        except Exception as exc:
            logger.warning("logo download %s failed: %s", url, exc)
            return None
        sp.set(status=resp.status_code, bytes=len(resp.content or b""))
    if resp.status_code != 200:
        logger.warning("logo download %s -> HTTP %s", url, resp.status_code)
        return None
    return resp.content


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

    with span("logo.generate", network=network, tmdb=tmdb_id):
        LOGO_DIR.mkdir(parents=True, exist_ok=True)
        networks = await _tmdb_tv_networks(settings, tmdb_id)
        chosen = _pick_network(networks, network) or {}
        logo_path = chosen.get("logo_path")
        network_id = chosen.get("id")
        logger.info("logo[%s]: chosen network=%r id=%s logo_path=%s",
                    network, chosen.get("name"), network_id, logo_path)

        raw = None
        if logo_path and not logo_path.lower().endswith(".svg"):
            raw = await _download(f"{TMDB_IMG}/w300{logo_path}")
        else:
            # Primary logo is SVG (or missing): prefer a raster alternative from the
            # network's image list; only if there's none do we rasterize the SVG.
            alt = await _network_raster_logo(settings, network_id)
            if alt:
                raw = await _download(f"{TMDB_IMG}/w300{alt}")
            elif logo_path:
                svg = await _download(f"{TMDB_IMG}/original{logo_path}")
                raw = _rasterize_svg(svg) if svg else None

        img = _render_tile(raw) if raw else None
        if img is None:
            logger.info("logo[%s]: no usable raster logo -> negative marker", network)
            _none_path(network).write_text("", encoding="utf-8")
            return None
        img.save(tile)
        logger.info("logo[%s]: GENERATED -> %s", network, tile.name)
    return tile


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
    with span("logos.ensure_logos", n=len(want)):
        results = await asyncio.gather(
            *(ensure_logo(settings, name, tmdb) for name, tmdb in want.items()),
            return_exceptions=True,
        )
    generated = 0
    for name, result in zip(want, results):
        if isinstance(result, Exception):
            logger.warning("logo pre-warm failed for %s: %s", name, result)
        elif result is not None:
            generated += 1
    return generated
