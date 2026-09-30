"""Each service's own video list, read into the one `Video` shape.

YAGA IS THE CASE THIS EXISTS FOR (Trakt 319458). Trakt sets no `trailer` on it,
and its only videos are a teaser and a sneak peek — so a panel reading the one
trailer field showed nothing, before a premiere, which is when somebody opening
it most wants a video. The payloads below are the two services' answers for it,
recorded 2026-09-30 and cut to the fields read.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.providers.base import Media, Video, VideoKind
from app.providers.simkl import detail as simkl_detail
from app.providers.trakt import detail as trakt_detail

SETTINGS = SimpleNamespace(trakt_client_id="cid", simkl_client_id="cid",
                           simkl_access_token="", cache_ttl_minutes=10,
                           timezone="UTC")

# GET /shows/319458/videos
YAGA_TRAKT = [
    {"title": "Official Teaser", "url": "https://youtube.com/watch?v=4PPodH3i_7k",
     "site": "youtube", "type": "teaser", "size": 1080, "official": True,
     "published_at": "2026-07-24T16:00:00.000Z", "country": "us", "language": "en"},
    {"title": "Official Sneak Peek", "url": "https://youtube.com/watch?v=abP6JNAZjHA",
     "site": "youtube", "type": "clip", "size": 1080, "official": True,
     "published_at": "2026-09-12T16:00:00.000Z", "country": "us", "language": "en"},
]

# The same title's `trailers`, as Simkl's catalogue record stores them.
YAGA_SIMKL = [
    {"name": "Official Teaser", "youtube": "4PPodH3i_7k", "size": 1080},
    {"name": "Official Sneak Peek", "youtube": "abP6JNAZjHA", "size": 1080},
]


class TraktListTests(unittest.TestCase):
    def test_yagas_two_videos_are_read_with_their_own_kinds(self):
        teaser, peek = trakt_detail.videos_from(YAGA_TRAKT)
        self.assertEqual(teaser, Video(
            site="youtube", video_id="4PPodH3i_7k",
            url="https://www.youtube.com/watch?v=4PPodH3i_7k",
            title="Official Teaser", kind=VideoKind.TEASER, official=True,
            published="2026-07-24T16:00:00.000Z", sources=("trakt",)))
        self.assertEqual((peek.video_id, peek.kind), ("abP6JNAZjHA", VideoKind.CLIP))

    def test_every_measured_type_is_in_the_vocabulary(self):
        for kind in ("trailer", "teaser", "clip", "featurette",
                     "behind the scenes", "opening credits"):
            with self.subTest(kind=kind):
                self.assertEqual(VideoKind.read(kind), VideoKind(kind))
        # A type Trakt adds later is still a video today.
        self.assertIs(VideoKind.read("recap"), VideoKind.VIDEO)

    def test_a_vimeo_video_is_kept_as_a_link_and_is_not_embeddable(self):
        [video] = trakt_detail.videos_from([
            {"title": "Trailer", "url": "https://vimeo.com/123456", "site": "vimeo",
             "type": "trailer", "official": False}])
        self.assertEqual((video.site, video.video_id, video.url),
                         ("vimeo", "", "https://vimeo.com/123456"))
        self.assertFalse(video.embeddable)

    def test_what_names_no_video_is_skipped_rather_than_guessed(self):
        """A YouTube address the strict reading cannot place, and a link out in a
        scheme nobody should be sent to, are both dropped."""
        self.assertEqual(trakt_detail.videos_from([
            {"url": "https://youtu.be/EhV", "site": "youtube", "type": "trailer"},
            {"url": "javascript:alert(1)", "site": "vimeo", "type": "trailer"},
            "not a video",
        ]), [])
        self.assertEqual(trakt_detail.videos_from(None), [])


class TraktFetchTests(unittest.IsolatedAsyncioTestCase):
    async def _fetch(self, media, **kwargs):
        get = AsyncMock(return_value=YAGA_TRAKT)
        with patch("app.providers.trakt.transport.cached_get", new=get):
            got = await trakt_detail.fetch_videos(SETTINGS, media, "319458", **kwargs)
        return got, get

    async def test_it_is_its_own_public_cached_call(self):
        got, get = await self._fetch("show")
        self.assertEqual(len(got), 2)
        (_client, _settings, path, _params), kwargs = get.call_args
        self.assertEqual(path, "shows/319458/videos")
        # The answer is the same whoever asks, so it rides the shared cache.
        self.assertNotIn("private", kwargs)

    async def test_a_film_asks_the_films_path_and_cache_only_is_passed_through(self):
        _got, get = await self._fetch("movie", cache_only=True)
        self.assertEqual(get.call_args.args[2], "movies/319458/videos")
        self.assertTrue(get.call_args.kwargs["cache_only"])

    async def test_the_description_carries_the_list_and_not_the_trailer_field(self):
        """Yaga's title record has no `trailer`; its description still carries
        both videos, from the list."""
        async def _get(client, settings, path, params=None, **kwargs):
            if path.endswith("/videos"):
                return YAGA_TRAKT
            if path == "shows/319458":
                return {"title": "Yaga", "trailer": None}
            return None

        with patch("app.providers.trakt.transport.cached_get",
                   new=AsyncMock(side_effect=_get)):
            got = await trakt_detail.fetch_details(SETTINGS, "show", "319458", None)
        self.assertNotIn("trailer", got)
        self.assertEqual([v.video_id for v in got["videos"]],
                         ["4PPodH3i_7k", "abP6JNAZjHA"])


class SimklListTests(unittest.TestCase):
    def test_yagas_two_videos_take_their_kind_from_their_names(self):
        teaser, peek = simkl_detail.videos_from(YAGA_SIMKL)
        self.assertEqual(teaser, Video(
            site="youtube", video_id="4PPodH3i_7k",
            url="https://www.youtube.com/watch?v=4PPodH3i_7k",
            title="Official Teaser", kind=VideoKind.TEASER, official=None,
            published=None, sources=("simkl",)))
        # "Sneak Peek" says neither trailer nor teaser, so it is a video.
        self.assertIs(peek.kind, VideoKind.VIDEO)

    def test_a_teaser_trailer_is_a_teaser_and_a_trailer_is_a_trailer(self):
        kinds = [v.kind for v in simkl_detail.videos_from([
            {"name": "Teaser Trailer", "youtube": "dQw4w9WgXcQ"},
            {"name": "Official Trailer 2", "youtube": "4PPodH3i_7k"},
            {"name": "", "youtube": "abP6JNAZjHA"}])]
        self.assertEqual(kinds, [VideoKind.TEASER, VideoKind.TRAILER, VideoKind.VIDEO])


class SimklFetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_list_is_read_off_the_stored_catalogue_record(self):
        record = AsyncMock(return_value={"trailers": YAGA_SIMKL, "overview": "x"})
        with patch("app.providers.simkl.titles.fetch_title", new=record):
            got = await simkl_detail.fetch_videos(SETTINGS, Media.SHOW, 2700000,
                                                  cache_only=True)
        self.assertEqual([v.video_id for v in got], ["4PPodH3i_7k", "abP6JNAZjHA"])
        self.assertTrue(record.call_args.kwargs["cache_only"])

    async def test_a_title_simkl_does_not_know_has_no_videos(self):
        with patch("app.providers.simkl.titles.fetch_title",
                   new=AsyncMock(return_value=None)):
            self.assertEqual(await simkl_detail.fetch_videos(
                SETTINGS, Media.MOVIE, 1), [])


if __name__ == "__main__":
    unittest.main()
