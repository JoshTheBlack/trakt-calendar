"""The ratings seed reads seasons as well as shows and films.

Trakt's /sync/ratings answers shows, seasons, episodes and films in one list. A
show or a film is a whole title; a SEASON is a season row of its show, carrying
its own score, and it arrives through the board's arrival rule like every other
add — over a whole-show row it takes that row over in place. An episode is not
something a board holds. The real Trakt adapter runs here with only the call at
its edge replaced, so the entry shapes are Trakt's own.
"""
from __future__ import annotations

import asyncio
from unittest import mock

from app import db
from app.auth import trakt_routes
from app.providers.trakt import TraktError, sync as trakt_sync
from app.ranker import core as ranker, sources as ranker_sources
from app.ranker.sources import SourceUnavailable
from tests.ranker.test_standalone import StandaloneTestCase, _token_for

SHOW = {"title": "The Bear", "year": 2022,
        "ids": {"trakt": 1, "tmdb": 136315, "imdb": "tt14452776", "slug": "the-bear"}}
OTHER = {"title": "Severance", "year": 2022, "ids": {"trakt": 2, "tmdb": 95396}}

ENTRIES = [
    {"type": "show", "rating": 7, "rated_at": "2026-01-01T00:00:00Z", "show": OTHER},
    {"type": "season", "rating": 10, "rated_at": "2026-01-02T00:00:00Z",
     "season": {"number": 2, "ids": {"trakt": 99}}, "show": SHOW},
    {"type": "season", "rating": 8, "rated_at": "2026-01-03T00:00:00Z",
     "season": {"number": 3, "ids": {"trakt": 98}}, "show": SHOW},
    {"type": "episode", "rating": 9, "rated_at": "2026-01-04T00:00:00Z",
     "episode": {"season": 2, "number": 7, "ids": {"trakt": 5}}, "show": SHOW},
    {"type": "movie", "rating": 6, "rated_at": "2026-01-05T00:00:00Z",
     "movie": {"title": "A Film", "year": 2020, "ids": {"trakt": 3, "tmdb": 550}}},
    # A season entry that does not say which season names nothing to hold.
    {"type": "season", "rating": 5, "season": {}, "show": SHOW},
]


def _answer(entries):
    async def _call(settings):
        return entries
    return _call


class RatingsSeedSeasonTests(StandaloneTestCase):

    def setUp(self):
        super().setUp()
        asyncio.run(ranker.create_board(self.user_id, uid="b1"))
        for patch in (mock.patch.object(trakt_routes, "access_token_for_user",
                                        _token_for("own-token")),
                      mock.patch.object(trakt_sync, "fetch_ratings", _answer(ENTRIES))):
            patch.start()
            self.addCleanup(patch.stop)

    def rows(self):
        return [(r["match_id"], r["season"], r["user_rating"], r["category_uid"])
                for r in asyncio.run(db.fetch_all(
                    "SELECT i.match_id, i.season, i.user_rating, c.uid AS category_uid "
                    "  FROM tier_items i LEFT JOIN tier_categories c ON c.id = i.category_id "
                    " ORDER BY i.match_id, IFNULL(i.season, -1)"))]

    def test_the_source_reads_seasons_and_skips_episodes(self):
        rated = asyncio.run(ranker_sources.ratings_source().fetch_ratings(self.user_id))
        self.assertEqual(
            [(r.title.media, r.title.ids.get("tmdb"), r.title.season, r.rating) for r in rated],
            [("show", 95396, None, 7), ("show", 136315, 2, 10), ("show", 136315, 3, 8),
             ("movie", 550, None, 6)])
        self.assertEqual(rated[1].title.title, "The Bear")

    def test_a_trakt_failure_reaches_the_board_as_the_neutral_one(self):
        """The board knows no provider's exception type — the seams exist so it
        need not — so a Trakt refusal is translated at the adapter's edge, keeping
        the status that lets a 401 read as a credential problem."""
        async def refuse(settings):
            raise TraktError("Trakt rejected the credentials", 401)

        with mock.patch.object(trakt_sync, "fetch_ratings", refuse):
            with self.assertRaises(SourceUnavailable) as caught:
                asyncio.run(ranker_sources.ratings_source().fetch_ratings(self.user_id))
        self.assertEqual(caught.exception.status, 401)
        self.assertIsInstance(caught.exception.__cause__, TraktError)

    def test_an_account_with_no_link_is_never_read_with_anybody_elses_token(self):
        with mock.patch.object(trakt_routes, "access_token_for_user", _token_for(None)),              mock.patch.object(trakt_sync, "fetch_ratings",
                               mock.AsyncMock(side_effect=AssertionError("read"))):
            with self.assertRaises(ranker_sources.NoLinkedIdentity):
                asyncio.run(ranker_sources.ratings_source().fetch_ratings(self.user_id))

    def test_a_seed_places_season_rows_with_their_own_scores(self):
        page = self.client.post("/rankings/boards/b1/seed", json={"commit": "1"}).text
        self.assertIn("Seeded 4 title(s)", page)
        self.assertEqual(self.rows(), [
            ("136315", 2, 10, "tier-s"),
            ("136315", 3, 8, "tier-b"),
            ("550", None, 6, "tier-d"),
            ("95396", None, 7, "tier-c"),
        ])

    def test_a_rated_season_takes_over_a_whole_show_row(self):
        asyncio.run(ranker.add_titles(self.user_id, "b1", [
            {"media": "show", "match_source": "tmdb", "match_id": "136315",
             "title": "The Bear"}]))
        whole_id = self.value("SELECT id FROM tier_items")
        preview = self.client.post("/rankings/boards/b1/seed", json={}).text
        self.assertIn("This would add 3 title(s), and make 1 whole-show row into the season",
                      preview)
        self.client.post("/rankings/boards/b1/seed", json={"commit": "1"})
        # The latest rated season took the row; the other arrived beside it.
        self.assertEqual(self.value("SELECT season FROM tier_items WHERE id = ?", (whole_id,)), 3)
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_items WHERE match_id = '136315'"), 2)
        self.assertEqual(self.value("SELECT user_rating FROM tier_items WHERE id = ?",
                                    (whole_id,)), 8)
