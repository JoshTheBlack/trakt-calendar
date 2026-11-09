"""A film on a rankings board offers its whole collection, and adding it adds
every film TMDB files under that collection.

THE PROMISES:
  - OFFERED ONLY FOR A FILM TMDB DESCRIBES AS ONE OF A COLLECTION, in that film's
    details panel; never for a show, nor a film in none.
  - THE REQUEST NAMES A BOARD ROW AND NOTHING ELSE: the collection is TMDB's
    description of that film, its films TMDB's own list (one cached request), so
    a page cannot hand the board a title.
  - THE FILMS ARRIVE THROUGH THE BOARD'S ONE ADD PATH, its arrival rule and its
    cap; a film already held adds nothing. Their titles are registered with the
    title store in bounded turns BEFORE the add's own transaction.

No network: TMDB's cached read is a table of answers (the real collection saved
under tests/fixtures/tmdb), and anything else it is asked fails the test.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from app import metadata
from app.config import Settings
from app.metadata import descriptions
from app.providers.tmdb import transport as tmdb_transport
from app.ranker import core as ranker
from tests.metadata.test_panel import _answer
from tests.providers.tmdb.test_normalize import fixture
from tests.support import AppTestCase, ORIGIN

BOARD = "b1"
DUNE_TWO = "693134"
COLLECTION = {"id": 726871, "name": "Dune Collection"}


class _Tmdb:
    """TMDB's cached read: the collection, or TMDB down, and every path recorded."""

    def __init__(self, *, down=False):
        self.down = down
        self.asked: list[str] = []

    async def __call__(self, settings, path, params=None, **kw):
        self.asked.append(path)
        if self.down:
            raise tmdb_transport.TmdbError("unreachable")
        if path == "collection/726871":
            return fixture("collection_dune")
        raise AssertionError(f"TMDB was asked for {path!r}, which nothing here answers")


class _CollectionTestCase(AppTestCase):

    def make_settings(self):
        return Settings(public_base_url=ORIGIN, tmdb_api_key="eyJa.eyJb.c",
                        simkl_public_calendar_enabled=False)

    def setUp(self):
        super().setUp()
        self.make_user("admin_user", is_admin=True)
        self.user_id = self.make_user("ranker", ranker_approved=True)
        self.sign_in_as(self.user_id)
        asyncio.run(ranker.create_board(self.user_id, uid=BOARD, name="Top"))
        self.tmdb = _Tmdb()
        patched = patch.object(tmdb_transport, "cached_get", self.tmdb)
        patched.start()
        self.addCleanup(patched.stop)

    def add_film(self, tmdb: str, *, collection=COLLECTION, title="Dune: Part Two") -> str:
        """A film row on the board, with TMDB's description of it in the store."""
        asyncio.run(ranker.add_titles(self.user_id, BOARD, [{
            "media": "movie", "match_source": "tmdb", "match_id": tmdb, "tmdb": int(tmdb),
            "title": title}]))
        asyncio.run(descriptions.store("tmdb", "movie", tmdb, _answer(
            "tmdb", tmdb, None, title=title, collection=collection or {}, season_count=0),
            asked_season=None, tz=ZoneInfo("UTC")))
        return f"movie:tmdb:{tmdb}"

    def panel(self, key: str) -> str:
        response = self.client.get("/rankings/fragments/details?"
                                   + urlencode({"board": BOARD, "key": key}))
        self.assertEqual(response.status_code, 200, response.text)
        return response.text

    def add_collection(self, key: str, **extra):
        response = self.client.post(f"/rankings/boards/{BOARD}/items/collection",
                                    json={"key": key, **extra})
        self.assertEqual(response.status_code, 200, response.text)
        return response.text

    def held(self) -> set[str]:
        board = asyncio.run(ranker.fetch_board(self.user_id, BOARD))
        return {item["key"] for item in board["pool"]}


class TheOfferTests(_CollectionTestCase):

    def test_a_film_tmdb_files_under_a_collection_offers_it(self):
        body = self.panel(self.add_film(DUNE_TWO))
        self.assertIn(f"/rankings/boards/{BOARD}/items/collection", body)
        self.assertIn("Add all of Dune Collection", body)
        self.assertIn('id="detailsActions" hx-swap-oob="innerHTML"', body)

    def test_a_film_in_no_collection_offers_nothing(self):
        body = self.panel(self.add_film("1", collection={}, title="Alone"))
        self.assertNotIn("items/collection", body)
        self.assertEqual(self.tmdb.asked, [], "opening the panel asks TMDB for no collection")


class TheAddTests(_CollectionTestCase):

    def test_every_film_of_the_collection_arrives_once(self):
        key = self.add_film(DUNE_TWO)
        body = self.add_collection(key)
        self.assertIn("Added 2 films of Dune Collection to the pool.", body)
        self.assertEqual(self.held(), {key, "movie:tmdb:438631", "movie:tmdb:1170608"})
        self.assertEqual(self.tmdb.asked, ["collection/726871"])

    def test_adding_it_again_adds_nothing_and_says_so(self):
        key = self.add_film(DUNE_TWO)
        self.add_collection(key)
        body = self.add_collection(key)
        self.assertIn("Every film of Dune Collection is already on this board.", body)
        self.assertEqual(len(self.held()), 3)

    def test_a_title_the_page_sends_is_ignored(self):
        """The row is the reference; nothing else in the request reaches the board."""
        key = self.add_film(DUNE_TWO)
        self.add_collection(key, title="Injected", ids={"tmdb": 1}, collection={"id": 5})
        self.assertNotIn("movie:tmdb:1", self.held())
        self.assertEqual(self.tmdb.asked, ["collection/726871"])

    def test_a_row_the_board_does_not_hold_adds_nothing(self):
        body = self.add_collection("movie:tmdb:999")
        self.assertIn("No title", body)
        self.assertEqual(self.held(), set())
        self.assertEqual(self.tmdb.asked, [])

    def test_a_film_in_no_collection_is_refused_with_a_sentence(self):
        key = self.add_film("1", collection={}, title="Alone")
        body = self.add_collection(key)
        self.assertIn("TMDB doesn&#39;t list this film as part of a collection.", body)
        self.assertEqual(self.held(), {key})

    def test_tmdb_down_adds_nothing_and_says_so(self):
        key = self.add_film(DUNE_TWO)
        self.tmdb.down = True
        body = self.add_collection(key)
        self.assertIn("Couldn&#39;t reach TMDB for the collection", body)
        self.assertEqual(self.held(), {key})

    def test_the_films_are_registered_in_turns_before_the_add(self):
        key = self.add_film(DUNE_TWO)
        turns = []
        real = metadata.ensure_many

        async def counted(filings, **kw):
            turns.append(len(filings))
            return await real(filings, **kw)

        with patch.object(ranker, "REGISTER_TURN_TITLES", 2), \
                patch.object(ranker.metadata, "ensure_many", counted), \
                patch.object(ranker.metadata, "register",
                             side_effect=AssertionError("registered inside the add")):
            self.add_collection(key)
        self.assertEqual(turns, [2, 1])
        self.assertEqual(len(self.held()), 3)
