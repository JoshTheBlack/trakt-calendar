"""GET /api/posters/missed — where a picture that did not load sends the browser.

PUBLIC, because the request arrives as the image proxy's redirect and Firefox
does not send the SameSite=Lax session cookie on it (measured); what it may act
on is decided by the signed token it carries (app/metadata/pictures.py, which
says why each part of this is shaped as it is).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from ..auth import AuthLevel
from ..authz import Guard
from ..config import load_settings
from . import pictures, refresh

logger = logging.getLogger(__name__)

router = APIRouter()
guard = Guard(router)

# NEVER STORED: each report is one view of one failing picture, and the answer
# — what to draw next — changes as soon as the confirmation behind it acts.
_NO_STORE = {"Cache-Control": "no-store"}


@guard.get(pictures.ROUTE, AuthLevel.PUBLIC)
async def picture_missed(request: Request):
    """The next picture, at once (pictures.answer), and the report confirmed
    behind it as a task of its own (pictures.confirm) — the browser waits for
    neither a HEAD nor a refresh.

    ONLY AN IMAGE ASKS HERE: a browser says what a request is for in
    Sec-Fetch-Dest, and a report that is not an image loading is not a picture
    that failed to load — a link followed, a script's fetch, a crawler — so it
    is refused before the token is read."""
    if (request.headers.get("sec-fetch-dest") or "").lower() != "image":
        logger.info("pictures: report refused — not an image's request (Sec-Fetch-Dest %r)",
                    request.headers.get("sec-fetch-dest") or "")
        return Response(status_code=400, headers=_NO_STORE)
    settings = load_settings()
    location, report = await pictures.answer(settings, request.query_params.get("t") or "")
    if report is not None:
        refresh.start(pictures.confirm(settings, report))
    return RedirectResponse(location, status_code=302, headers=_NO_STORE)
