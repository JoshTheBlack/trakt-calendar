"""Shared TMDB API client: auth branching (v3 api_key vs v4 bearer token) plus a
bare GET-JSON / GET-bytes helper. Everything that talks to TMDB — network logos
(app/logos.py) and poster art (app/posters.py) — goes through this rather than
each re-deriving the auth branch.
"""
from __future__ import annotations

import logging

from .perftrace import span

logger = logging.getLogger(__name__)

API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p"


def _is_v4_token(key: str) -> bool:
    # TMDB v4 read tokens are long JWTs ("eyJ..."); v3 keys are short hex.
    return key.startswith("eyJ") or len(key) > 60


async def get_json(settings, path: str, label: str) -> dict | None:
    """GET a TMDB API path (auth via v4 bearer or v3 api_key). Returns parsed
    JSON, or None on any failure — network error, non-200, or an unparsable
    body — so a caller can degrade rather than raise."""
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
            resp = await shared_client().get(f"{API}{path}", params=params, headers=headers)
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
    from .trakt import shared_client
    with span("tmdb.download") as sp:
        try:
            resp = await shared_client().get(url)
        except Exception as exc:
            logger.warning("download %s failed: %s", url, exc)
            return None
        sp.set(status=resp.status_code, bytes=len(resp.content or b""))
    if resp.status_code != 200:
        logger.warning("download %s -> HTTP %s", url, resp.status_code)
        return None
    return resp.content
