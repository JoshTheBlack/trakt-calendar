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

from app import auth, db
from app.auth import trakt_routes
from app.providers.base import Media
from app.providers.tmdb import account as tmdb_account
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
        source = asyncio.run(ranker_sources.ratings_source(self.user_id, "trakt"))
        rated = asyncio.run(source.fetch_ratings(self.user_id))
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
                asyncio.run(ranker_sources.TraktRatingsSource().fetch_ratings(self.user_id))
        self.assertEqual(caught.exception.status, 401)
        self.assertIsInstance(caught.exception.__cause__, TraktError)

    def test_an_account_with_no_link_is_never_read_with_anybody_elses_token(self):
        with mock.patch.object(trakt_routes, "access_token_for_user", _token_for(None)),              mock.patch.object(trakt_sync, "fetch_ratings",
                               mock.AsyncMock(side_effect=AssertionError("read"))):
            with self.assertRaises(ranker_sources.NoLinkedIdentity):
                asyncio.run(ranker_sources.TraktRatingsSource().fetch_ratings(self.user_id))
            # Nor is it offered: no linked service keeps ratings for this account.
            with self.assertRaises(ranker_sources.UnknownRatingsService):
                asyncio.run(ranker_sources.ratings_source(self.user_id, "trakt"))

    def test_a_seed_places_season_rows_with_their_own_scores(self):
        page = self.client.post("/rankings/boards/b1/seed",
                                json={"commit": "1", "service": "trakt"}).text
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
        preview = self.client.post("/rankings/boards/b1/seed", json={"service": "trakt"}).text
        self.assertIn("This would add 3 title(s), and make 1 whole-show row into the season",
                      preview)
        self.client.post("/rankings/boards/b1/seed", json={"commit": "1", "service": "trakt"})
        # The latest rated season took the row; the other arrived beside it.
        self.assertEqual(self.value("SELECT season FROM tier_items WHERE id = ?", (whole_id,)), 3)
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_items WHERE match_id = '136315'"), 2)
        self.assertEqual(self.value("SELECT user_rating FROM tier_items WHERE id = ?",
                                    (whole_id,)), 8)


TMDB_RATED = [
    (Media.SHOW, {"id": 136315, "name": "The Bear", "first_air_date": "2022-06-23",
                  "account_rating": {"value": 8.5, "created_at": "2026-11-08T13:05:39Z"}}),
    (Media.SHOW, {"id": 95396, "name": "Severance", "first_air_date": "2022-02-17",
                  "account_rating": {"value": 7}}),
    (Media.MOVIE, {"id": 550, "title": "Fight Club", "release_date": "1999-10-15",
                   "account_rating": {"value": 0.5}}),
    # An item with no score names nothing to seed.
    (Media.MOVIE, {"id": 551, "title": "Unscored"}),
]


class TmdbRatingsSeedTests(StandaloneTestCase):
    """A board seeded from the viewer's own TMDB ratings — a choice they make
    among the services they have linked that keep ratings, never a fallback."""

    def setUp(self):
        super().setUp()
        asyncio.run(ranker.create_board(self.user_id, uid="b1"))
        asyncio.run(db.run(lambda conn: auth.insert_linked_identity(
            conn, user_id=self.user_id, provider="tmdb",
            provider_user_id="6a5edaa7df66e2fc7c922663", access_token="tmdb-user-token")))
        self.asked = []

        async def ratings(settings):
            self.asked.append((settings.tmdb_user_token, settings.tmdb_account_id))
            return TMDB_RATED

        started = mock.patch.object(tmdb_account, "ratings", ratings)
        started.start()
        self.addCleanup(started.stop)

    def test_every_registered_service_has_a_ratings_entry(self):
        from app import providers
        for source in providers.registered():
            with self.subTest(source=str(source)):
                ranker_sources.has_ratings(str(source))      # a KeyError fails it

    def test_tmdb_is_offered_once_linked_and_trakt_is_not_without_a_link(self):
        self.assertEqual(asyncio.run(ranker_sources.ratings_services(self.user_id)),
                         [("tmdb", "TMDB")])

    def test_both_are_offered_when_both_are_linked_in_the_declared_order(self):
        with mock.patch.object(trakt_routes, "access_token_for_user", _token_for("own-token")):
            self.assertEqual(asyncio.run(ranker_sources.ratings_services(self.user_id)),
                             [("tmdb", "TMDB"), ("trakt", "Trakt")])

    def test_a_service_the_viewer_cannot_seed_from_is_refused(self):
        for name in ("trakt", "simkl", "letterboxd", ""):
            with self.subTest(name=name), self.assertRaises(ranker_sources.UnknownRatingsService):
                asyncio.run(ranker_sources.ratings_source(self.user_id, name))

    def test_the_read_carries_the_viewers_own_grant(self):
        source = asyncio.run(ranker_sources.ratings_source(self.user_id, "tmdb"))
        rated = asyncio.run(source.fetch_ratings(self.user_id))
        self.assertEqual(self.asked, [("tmdb-user-token", "6a5edaa7df66e2fc7c922663")])
        # Half points round up onto a board's whole 1-10 scale.
        self.assertEqual([(r.title.media, r.title.ids, r.title.year, r.rating) for r in rated],
                         [("show", {"tmdb": 136315}, 2022, 9),
                          ("show", {"tmdb": 95396}, 2022, 7),
                          ("movie", {"tmdb": 550}, 1999, 1)])

    def test_the_whole_score_rounds_a_half_up_and_refuses_what_is_not_a_score(self):
        cases = {8.5: 9, 8.0: 8, 0.5: 1, 10: 10, 9.5: 10, "7": 7,
                 0: None, -1: None, 10.5: None, None: None, "x": None}
        for given, wanted in cases.items():
            with self.subTest(given=given):
                self.assertEqual(ranker_sources._whole_score(given), wanted)

    def test_the_control_offers_one_press_per_service(self):
        with mock.patch.object(trakt_routes, "access_token_for_user", _token_for("own-token")):
            page = self.client.get("/rankings?board=b1").text
        self.assertIn("Seed from TMDB ratings", page)
        self.assertIn("Seed from Trakt ratings", page)

    def test_a_seed_from_tmdb_places_its_titles(self):
        preview = self.client.post("/rankings/boards/b1/seed", json={"service": "tmdb"}).text
        self.assertIn("This would add 3 title(s)", preview)
        self.assertIn('"service": "tmdb"', preview)
        page = self.client.post("/rankings/boards/b1/seed",
                                json={"commit": "1", "service": "tmdb"}).text
        self.assertIn("Seeded 3 title(s)", page)
        self.assertEqual(self.rows(), [("136315", None, 9, "tier-a"),
                                       ("550", None, 1, "tier-f"),
                                       ("95396", None, 7, "tier-c")])

    def test_a_seed_naming_another_service_reads_nothing(self):
        resp = self.client.post("/rankings/boards/b1/seed", json={"service": "trakt"})
        self.assertIn("no trakt ratings", resp.text)
        self.assertEqual(self.asked, [])
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_items"), 0)

    def rows(self):
        return RatingsSeedSeasonTests.rows(self)

    def test_a_seed_of_many_titles_registers_them_in_bounded_turns(self):
        """A ratings list is hundreds of titles: registering them is turns of
        ensure_many's fixed shape, before the seed's own transaction."""
        many = [(Media.MOVIE, {"id": 1000 + n, "title": f"Film {n}",
                               "account_rating": {"value": 6}}) for n in range(450)]
        turns = []
        real = ranker.metadata.ensure_many

        async def counted(titles, **kwargs):
            titles = list(titles)
            turns.append(len(titles))
            return await real(titles, **kwargs)

        async def ratings(settings):
            return many

        with mock.patch.object(tmdb_account, "ratings", ratings), \
             mock.patch.object(ranker.metadata, "ensure_many", counted):
            self.client.post("/rankings/boards/b1/seed", json={"commit": "1", "service": "tmdb"})
        self.assertEqual(turns, [ranker.REGISTER_TURN_TITLES, ranker.REGISTER_TURN_TITLES, 50])
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_items"), 450)
