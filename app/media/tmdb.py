"""Shared TMDB API client: auth branching (v3 api_key vs v4 bearer token) plus a
bare GET-JSON / GET-bytes helper. Everything that talks to TMDB — network logos
(app/media/logos.py) and poster art (app/media/posters.py) — goes through this
rather than each re-deriving the auth branch.
"""
from __future__ import annotations

import logging

from .. import http_pool
from ..perftrace import span

logger = logging.getLogger(__name__)

API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p"

# TMDB'S OWN POOL. This module used to reach into the Trakt provider package for
# a client, which put two unrelated services on one set of connections: a poster
# warm pulling eight images at once could hold every slot while a calendar
# refresh waited, and neither service's limits had anything to do with the other's.
# TMDB publishes no per-second cap worth gating on, so there is no concurrency
# gate here — the fan-out bound that matters lives at the call site
# (app/media/posters.py caps how many tiles it resolves at all).
#
# ONE POOL FOR THE API AND THE IMAGE CDN, deliberately: httpx pools per host
# internally, so api.themoviedb.org and image.tmdb.org already get their own
# connections out of it, and splitting them here would buy a second set of
# knobs for the same behaviour.
POOL = http_pool.Pool("tmdb", max_connections=8, timeout=20)


def _is_v4_token(key: str) -> bool:
    # TMDB v4 read tokens are long JWTs ("eyJ..."); v3 keys are short hex.
    return key.startswith("eyJ") or len(key) > 60


async def get_json(settings, path: str, label: str) -> dict | None:
    """GET a TMDB API path (auth via v4 bearer or v3 api_key). Returns parsed
    JSON, or None on any failure — network error, non-200, or an unparsable
    body — so a caller can degrade rather than raise."""
    key = (settings.tmdb_api_key or "").strip()
    headers, params = {}, {}
    if _is_v4_token(key):
        headers["Authorization"] = f"Bearer {key}"
    else:
        params["api_key"] = key
    auth = "v4/bearer" if _is_v4_token(key) else "v3/api_key"
    with span(label, path=path, auth=auth) as sp:
        try:
            resp = await POOL.client().get(f"{API}{path}", params=params, headers=headers)
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


async def download(url: str) -> bytes | None:
    """GET raw bytes from any URL — a TMDB image, or a previously-recorded
    registry URL from another provider. No auth: TMDB's image CDN and Trakt's
    poster URLs are both unauthenticated. None on any failure."""
    with span("tmdb.download") as sp:
        try:
            resp = await POOL.client().get(url)
        except Exception as exc:
            logger.warning("download %s failed: %s", url, exc)
            return None
        sp.set(status=resp.status_code, bytes=len(resp.content or b""))
    if resp.status_code != 200:
        logger.warning("download %s -> HTTP %s", url, resp.status_code)
        return None
    return resp.content
