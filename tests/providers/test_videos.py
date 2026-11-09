"""app/providers/videos.py — every service's videos for a title, pooled.

Pure: no transport is reached, so these build `Video`s directly.
"""
from __future__ import annotations

import unittest

from app.providers.base import Video, VideoKind
from app.providers.videos import pool, season_first


def _yt(video_id, kind=VideoKind.TRAILER, *, source="trakt", official=None,
        published=None, title=""):
    return Video(site="youtube", video_id=video_id,
                 url=f"https://www.youtube.com/watch?v={video_id}", title=title,
                 kind=kind, official=official, published=published,
                 sources=(source,))


def _ids(videos):
    return [v.video_id or v.url for v in videos]


class OrderTests(unittest.TestCase):
    def test_a_trailer_then_a_teaser_then_anything_else_whoever_lists_it(self):
        clip = _yt("clipclipcli", VideoKind.CLIP)
        teaser = _yt("teaserteas1", VideoKind.TEASER)
        trailer = _yt("trailertrai", source="simkl")
        self.assertEqual(_ids(pool([[clip, teaser], [trailer]], "trakt")),
                         ["trailertrai", "teaserteas1", "clipclipcli"])

    def test_within_a_kind_the_describing_service_comes_first(self):
        trakts = _yt("traktstrail")
        simkls = _yt("simklstrail", source="simkl")
        self.assertEqual(_ids(pool([[trakts], [simkls]], "simkl"))[0], "simklstrail")
        self.assertEqual(_ids(pool([[trakts], [simkls]], "trakt"))[0], "traktstrail")

    def test_then_official_before_not_then_the_newest(self):
        old = _yt("oldofficial", official=True, published="2025-01-01T00:00:00.000Z")
        new = _yt("newofficial", official=True, published="2026-05-01T00:00:00.000Z")
        fan = _yt("fanmadetrai", official=False, published="2026-09-01T00:00:00.000Z")
        undated = _yt("undatedoffi", official=True)
        self.assertEqual(_ids(pool([[fan, old, undated, new]], "trakt")),
                         ["newofficial", "oldofficial", "undatedoffi", "fanmadetrai"])

    def test_yaga_plays_its_teaser_and_offers_the_sneak_peek(self):
        """Trakt 319458: no trailer anywhere, a teaser and a clip on Trakt, and
        the same two on Simkl under names that say "teaser" and neither."""
        trakt = [_yt("4PPodH3i_7k", VideoKind.TEASER, official=True,
                     published="2026-07-24T16:00:00.000Z", title="Official Teaser"),
                 _yt("abP6JNAZjHA", VideoKind.CLIP, official=True,
                     published="2026-09-12T16:00:00.000Z", title="Official Sneak Peek")]
        simkl = [_yt("4PPodH3i_7k", VideoKind.TEASER, source="simkl", title="Official Teaser"),
                 _yt("abP6JNAZjHA", VideoKind.VIDEO, source="simkl",
                     title="Official Sneak Peek")]
        teaser, peek = pool([trakt, simkl], "trakt")
        self.assertEqual((teaser.video_id, peek.video_id), ("4PPodH3i_7k", "abP6JNAZjHA"))
        self.assertEqual(teaser.sources, ("trakt", "simkl"))
        # Trakt's description of the sneak peek, not Simkl's "a video".
        self.assertIs(peek.kind, VideoKind.CLIP)


class DedupeTests(unittest.TestCase):
    def test_one_video_two_services_list_is_one_row_naming_both(self):
        simkls = _yt("samevideo00", VideoKind.VIDEO, source="simkl", title="Trailer")
        trakts = _yt("samevideo00", official=True, published="2026-01-01T00:00:00.000Z",
                     title="Official Trailer")
        [video] = pool([[trakts], [simkls]], "simkl")
        self.assertEqual(video.sources, ("trakt", "simkl"))
        # The richer description is kept whichever service came first.
        self.assertEqual((video.title, video.kind, video.official),
                         ("Official Trailer", VideoKind.TRAILER, True))
        [again] = pool([[simkls], [trakts]], "simkl")
        self.assertEqual(again.title, "Official Trailer")
        self.assertEqual(again.sources, ("simkl", "trakt"))

    def test_one_service_listing_a_video_twice_lists_it_once(self):
        """Measured on Trakt's film lists: one video under two titles."""
        self.assertEqual(len(pool([[_yt("twicelisted", title="Trailer"),
                                    _yt("twicelisted", title="Trailer 2")]], "trakt")), 1)


class WhatPlaysTests(unittest.TestCase):
    VIMEO = Video(site="vimeo", video_id="", url="https://vimeo.com/123456",
                  title="Trailer", kind=VideoKind.TRAILER, official=True,
                  published="2026-09-01T00:00:00.000Z", sources=("trakt",))

    def test_a_link_out_never_plays_first_when_something_can_play(self):
        teaser = _yt("teaserteas1", VideoKind.TEASER)
        got = pool([[self.VIMEO, teaser]], "trakt")
        self.assertEqual(_ids(got), ["teaserteas1", "https://vimeo.com/123456"])
        self.assertFalse(got[1].embeddable)

    def test_with_nothing_playable_the_link_out_is_still_offered(self):
        self.assertEqual(pool([[self.VIMEO]], "trakt"), [self.VIMEO])

    def test_a_service_that_answered_nothing_contributes_nothing(self):
        """A service that failed is asked as an empty list by the caller, and
        the other service's videos are all still there."""
        self.assertEqual(_ids(pool([[], [_yt("onlysimkl00", source="simkl")]], "trakt")),
                         ["onlysimkl00"])
        self.assertEqual(pool([[], []], "trakt"), [])


if __name__ == "__main__":
    unittest.main()


class ASeasonsOwnVideosComeFirstTests(unittest.TestCase):
    """videos.season_first: a panel opened on a season offers that season's own
    videos ahead of the title's, by pool's own order, each video once."""

    def test_the_seasons_trailer_leads_and_the_titles_follow(self):
        season = [_yt("s1teaser111", VideoKind.TEASER, source="tmdb"),
                  _yt("s1trailer11", source="tmdb")]
        title = [_yt("s5trailer11", source="tmdb"), _yt("traktclip11", VideoKind.CLIP)]
        self.assertEqual(_ids(season_first(season, title, "tmdb")),
                         ["s1trailer11", "s1teaser111", "s5trailer11", "traktclip11"])

    def test_a_video_both_list_is_offered_once_where_the_season_put_it(self):
        shared = _yt("sharedtrail", source="tmdb")
        self.assertEqual(_ids(season_first([shared], [_yt("titletraile"), shared], "tmdb")),
                         ["sharedtrail", "titletraile"])

    def test_a_season_whose_own_cannot_play_here_plays_the_titles_first(self):
        vimeo = Video(site="vimeo", video_id="", url="https://vimeo.com/1", title="",
                      kind=VideoKind.TRAILER, official=None, published=None, sources=("tmdb",))
        self.assertEqual(_ids(season_first([vimeo], [_yt("titletraile")], "tmdb")),
                         ["titletraile", "https://vimeo.com/1"])

    def test_no_season_videos_is_the_titles_list(self):
        title = [_yt("titletraile"), _yt("titleteaser", VideoKind.TEASER)]
        self.assertEqual(_ids(season_first([], title, "trakt")), _ids(title))
