"""fetch_details' poster and cast headshots must never reach a browser as
Trakt's own address — this app's CSP img-src admits only the shared image
proxy (app/providers/base.py's proxied_image), and the details panel hands
both fields straight to an <img src>.

No network: transport.cached_get is patched, the seam every other detail test
in this package patches too.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.providers.trakt import detail

SETTINGS = SimpleNamespace(trakt_client_id="id", trakt_access_token="",
                           cache_ttl_minutes=10, timezone="UTC")

INFO = {
    "title": "A Movie", "ids": {"trakt": 99},
    "images": {"poster": ["walter.trakt.tv/images/movies/000/000/099/posters/original/abc.jpg"]},
}
PEOPLE = {
    "cast": [{"person": {"name": "Cast One",
                         "images": {"headshot": ["walter.trakt.tv/images/people/000/000/001/headshots/medium/def.jpg"]}},
              "character": "Lead"}],
}


class DetailImageProxyTests(unittest.IsolatedAsyncioTestCase):
    async def _details(self):
        async def _get(client, settings, path, params=None, **kwargs):
            return INFO if path == "movies/99" else PEOPLE

        with patch("app.providers.trakt.transport.cached_get", new=AsyncMock(side_effect=_get)):
            return await detail.fetch_details(SETTINGS, "movie", 99, None)

    async def test_the_poster_is_addressed_through_the_shared_proxy(self):
        got = await self._details()
        self.assertTrue(got["poster"].startswith("https://wsrv.nl/?url="))
        self.assertIn("walter.trakt.tv", got["poster"])

    async def test_a_cast_headshot_is_addressed_through_the_shared_proxy(self):
        got = await self._details()
        headshot = got["cast"][0]["headshot"]
        self.assertTrue(headshot.startswith("https://wsrv.nl/?url="))
        self.assertIn("walter.trakt.tv", headshot)


if __name__ == "__main__":
    unittest.main()
