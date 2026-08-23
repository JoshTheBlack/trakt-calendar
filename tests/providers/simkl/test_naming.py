"""Reading Simkl's per-title record for what a title IS — the rule both the
season picker and the library read share.

The measurements these cases are built from, taken live 2026-08-18 against
Simkl's public catalogue: "Shingeki no Kyojin" (simkl 39687), "...Season 2"
(439744) and "...Season 3" (694485) are three catalogue titles carrying ONE tmdb
id (1429), each numbering its own episodes from season 1 and stating which
season of the series it is in `mapped_tvdb_seasons`. One Piece (38636) maps onto
seasons 1-23 at once, which is the ambiguous shape.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.providers.simkl import _naming, transport

SETTINGS = SimpleNamespace(simkl_client_id="cid", simkl_access_token="",
                           cache_ttl_minutes=10)


class ReadTests(unittest.TestCase):
    """What one raw `GET /tv/{id}?extended=full` payload says."""

    def test_a_single_mapped_season_is_the_season_this_title_is(self):
        got = _naming.read({"ids": {"simkl": 694485, "tmdb": 1429},
                            "season": 3, "mapped_tvdb_seasons": [3]})
        self.assertEqual(got.season, 3)

    def test_a_mapping_naming_several_seasons_is_not_narrowed_to_one(self):
        """One Piece's shape. A title spanning seasons 1-23 is already numbering
        its episodes the way the show does, so there is nothing to translate —
        and picking one of them would invent a fact the mapping withheld."""
        got = _naming.read({"ids": {"simkl": 38636}, "mapped_tvdb_seasons": list(range(1, 24))})
        self.assertIsNone(got.season)

    def test_the_bare_season_field_answers_only_when_no_mapping_exists(self):
        """Ordinary TV titles carry no `mapped_tvdb_seasons` at all — measured —
        so `season` is the fallback for its ABSENCE, never for its ambiguity."""
        self.assertEqual(_naming.read({"ids": {"simkl": 1}, "season": 4}).season, 4)
        self.assertIsNone(_naming.read({"ids": {"simkl": 1}, "season": 4,
                                        "mapped_tvdb_seasons": [4, 5]}).season)

    def test_a_record_saying_neither_names_no_season(self):
        self.assertIsNone(_naming.read({"ids": {"simkl": 1}}).season)

    def test_ids_are_collect_ids_filtered_and_the_spelling_quirk_corrected(self):
        got = _naming.read({"ids": {"simkl_id": 439744, "tmdb": 1429, "tvdb": 99,
                                    "imdb": "tt1", "mal": 25777, "anilist": "x"}})
        self.assertEqual(got.ids, {"simkl": 439744, "tmdb": 1429, "tvdb": 99,
                                   "imdb": "tt1", "mal": 25777})

    def test_the_network_comes_off_the_same_record_and_defaults_to_empty(self):
        self.assertEqual(_naming.read({"ids": {"simkl": 1}, "network": " NHK "}).network, "NHK")
        self.assertEqual(_naming.read({"ids": {"simkl": 1}}).network, "")

    def test_a_body_that_is_not_a_record_reads_as_nothing_rather_than_raising(self):
        for payload in ([], None, "not a title"):
            with self.subTest(payload=payload):
                self.assertEqual(_naming.read(payload), _naming.EMPTY)


class TranslationTests(unittest.TestCase):
    """Which of a title's own season numbers means which season of the show."""

    def test_a_season_title_translates_its_own_season_one(self):
        """The Attack on Titan S3 shape: the title calls its episodes season 1
        and the record says they are the series' season 3."""
        naming = _naming.Naming(ids={"tmdb": 1429}, season=3, network="")
        self.assertEqual(_naming.translation(naming, [1]), (1, 3))

    def test_a_title_already_numbering_itself_correctly_needs_no_translation(self):
        naming = _naming.Naming(ids={"tmdb": 1429}, season=1, network="")
        self.assertIsNone(_naming.translation(naming, [1]))

    def test_a_title_spanning_several_of_its_own_seasons_is_left_alone(self):
        """Its numbering already IS the show's, so renumbering would move
        episodes that were in the right place to begin with."""
        naming = _naming.Naming(ids={"tmdb": 1429}, season=3, network="")
        self.assertIsNone(_naming.translation(naming, [1, 2, 3]))

    def test_a_title_stating_no_seasons_of_its_own_is_left_alone(self):
        naming = _naming.Naming(ids={"tmdb": 1429}, season=3, network="")
        self.assertIsNone(_naming.translation(naming, []))

    def test_a_record_that_names_no_season_is_left_alone(self):
        """The fallback every caller shares: missing and ambiguous arrive as the
        same None, so nothing downstream has to tell them apart."""
        self.assertIsNone(_naming.translation(_naming.EMPTY, [1]))


class SiblingTests(unittest.TestCase):
    """The `relations` block, filtered to what could be a season.

    Measured 2026-08-18: Attack on Titan's twelve relations include four films
    (`summary`), an OVA (`side story`) and an `alternative setting` spin-off
    beside the real sequels, and the block is TRANSITIVE — every member of a
    series named every other, across three series and eleven titles.
    """

    RECORD = {"ids": {"simkl": 39687, "tmdb": "1429"}, "mapped_tvdb_seasons": [1],
              "relations": [
                  {"relation_type": "sequel", "anime_type": "tv", "ids": {"simkl": 439744}},
                  {"relation_type": "summary", "anime_type": "movie", "ids": {"simkl": 49394}},
                  {"relation_type": "side story", "anime_type": "ova", "ids": {"simkl": 38688}},
                  {"relation_type": "sequel", "anime_type": "tv", "ids": {"simkl": 694485}},
                  {"relation_type": "sequel", "anime_type": "special", "ids": {"simkl": 1883416}},
              ]}

    def test_films_and_side_material_are_not_seasons(self):
        self.assertEqual(_naming.read(self.RECORD).siblings, (439744, 694485, 1883416))

    def test_a_record_with_no_relations_has_no_siblings(self):
        """Ordinary television — Gossip Girl carries an empty block."""
        self.assertEqual(_naming.read({"ids": {"simkl": 10452}}).siblings, ())


class TitleForSeasonTests(unittest.IsolatedAsyncioTestCase):
    """Which Simkl title holds a given season of a series, from any member."""

    FAMILY = {
        39687: {"ids": {"simkl": 39687, "tmdb": "1429"}, "mapped_tvdb_seasons": [1],
                "relations": [
                    {"anime_type": "tv", "ids": {"simkl": 439744}},
                    {"anime_type": "tv", "ids": {"simkl": 694485}},
                    # A different tracker row that shares the family: its own
                    # tmdb id is not 1429 (measured — 313599).
                    {"anime_type": "tv", "ids": {"simkl": 1120029}},
                    {"anime_type": "movie", "ids": {"simkl": 49394}}]},
        439744: {"ids": {"simkl": 439744, "tmdb": "1429"}, "mapped_tvdb_seasons": [2],
                 "relations": [{"anime_type": "tv", "ids": {"simkl": 39687}},
                               {"anime_type": "tv", "ids": {"simkl": 694485}}]},
        694485: {"ids": {"simkl": 694485, "tmdb": "1429"}, "mapped_tvdb_seasons": [3],
                 "relations": [{"anime_type": "tv", "ids": {"simkl": 39687}},
                               {"anime_type": "tv", "ids": {"simkl": 439744}}]},
        1120029: {"ids": {"simkl": 1120029, "tmdb": "313599"}, "mapped_tvdb_seasons": [2]},
    }

    async def _resolve(self, simkl_id, season, family=None):
        family = self.FAMILY if family is None else family
        asked = []

        async def _get(_client, _settings, path, _params=None, **_kwargs):
            asked.append(path)
            answer = family.get(int(path.split("/")[1]))
            if isinstance(answer, Exception):
                raise answer
            return answer

        with patch("app.providers.simkl.transport.cached_get", new=AsyncMock(side_effect=_get)):
            got = await _naming.title_for_season(SETTINGS, simkl_id, season)
        return got, asked

    async def test_the_title_that_already_is_the_season_answers_itself(self):
        got, asked = await self._resolve(694485, 3)
        self.assertEqual(got, 694485)
        self.assertEqual(asked, ["tv/694485"])

    async def test_a_sibling_holding_the_season_is_found_from_any_member(self):
        for start, season, want in ((39687, 3, 694485), (694485, 2, 439744),
                                    (439744, 1, 39687), (39687, 2, 439744)):
            with self.subTest(start=start, season=season):
                got, _asked = await self._resolve(start, season)
                self.assertEqual(got, want)

    async def test_a_sibling_of_a_different_tracker_row_is_refused(self):
        """`relations` crosses tracker identities: Attack on Titan's "The Final
        Season" is tmdb 313599, a different row, and it names a season 2 of its
        own. Answering with it would hand `show:tmdb:1429` another show's
        episodes."""
        got, _asked = await self._resolve(39687, 2)
        self.assertEqual(got, 439744)
        family = {**self.FAMILY,
                  439744: {**self.FAMILY[439744], "mapped_tvdb_seasons": [9]}}
        got, _asked = await self._resolve(39687, 2, family=family)
        self.assertIsNone(got)

    async def test_a_season_no_title_of_the_series_holds_answers_none(self):
        got, _asked = await self._resolve(39687, 7)
        self.assertIsNone(got)

    async def test_an_ordinary_show_with_no_siblings_costs_one_lookup(self):
        family = {10452: {"ids": {"simkl": 10452, "tmdb": "1395"}}}
        got, asked = await self._resolve(10452, 3, family=family)
        self.assertIsNone(got)
        self.assertEqual(asked, ["tv/10452"])

    async def test_no_id_to_start_from_costs_no_request(self):
        spy = AsyncMock()
        with patch("app.providers.simkl.transport.cached_get", new=spy):
            self.assertIsNone(await _naming.title_for_season(SETTINGS, None, 2))
        spy.assert_not_awaited()


class FetchTests(unittest.IsolatedAsyncioTestCase):
    """The one call, and how it fails."""

    async def _fetch(self, payload, simkl_id=694485):
        calls = []

        async def _get(client, settings, path, params=None, **kwargs):
            calls.append((path, params, kwargs))
            if isinstance(payload, Exception):
                raise payload
            return payload

        with patch("app.providers.simkl.transport.cached_get", new=AsyncMock(side_effect=_get)):
            return await _naming.fetch(SETTINGS, simkl_id), calls

    async def test_it_reads_the_extended_per_title_record_on_the_catalog_pool(self):
        _got, calls = await self._fetch({"ids": {"simkl": 694485}})
        (path, params, kwargs), = calls
        self.assertEqual(path, "tv/694485")
        self.assertEqual(params, {"extended": "full"})
        self.assertIs(kwargs["pool"], transport.CATALOG_POOL)
        # A catalogue record is the same for everybody, so it may be cached
        # globally — the flag that would say otherwise must be absent.
        self.assertNotIn("private", kwargs)

    async def test_a_title_with_no_id_costs_no_request(self):
        spy = AsyncMock()
        with patch("app.providers.simkl.transport.cached_get", new=spy):
            got = await _naming.fetch(SETTINGS, None)
        spy.assert_not_awaited()
        self.assertEqual(got, _naming.EMPTY)

    async def test_a_title_simkl_cannot_place_answers_empty(self):
        """Simkl's "not found" is a 200 whose body is not a title, so this has to
        read as "nothing known" rather than as a failure."""
        got, _calls = await self._fetch([])
        self.assertEqual(got, _naming.EMPTY)

    async def test_a_genuine_failure_raises_rather_than_reading_as_no_ids(self):
        """A caller resolving a bare search hit would otherwise be told the title
        has no ids at all, which is a different and wrong answer."""
        with self.assertRaises(transport.SimklError):
            await self._fetch(transport.SimklError("refused", 401))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
