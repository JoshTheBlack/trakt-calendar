"""app/providers/tmdb/detail.py and search.py, answered from TMDB's real payloads.

`transport.cached_get` is replaced by a table of paths to saved answers, so what
is held is what each verb asks for and how it reads the answer — including the
two answers that matter most once TMDB is first: a season TMDB does not hold is
an EMPTY season (so the next service answers it), and a TMDB that could not be
asked RAISES (so a tracker row keeps its last-known numbers).
"""
from __future__ import annotations

import unittest
from datetime import date
from unittest import mock

from app.config import Settings
from app.providers import tmdb as tmdb_provider
from app.providers.base import Media, Source
from app.providers.tmdb import detail, search, transport
from tests.providers.tmdb.test_normalize import DUNE, SEASON_2, SEVERANCE, fixture

SETTINGS = Settings(tmdb_api_key="eyJa.eyJb.c")


class _Tmdb:
    """Answers `cached_get` by path; a path with no answer is TMDB's 404, and
    `down` makes every call a failure to ask."""

    def __init__(self, answers=None, *, down=False):
        self.answers = answers or {}
        self.down = down
        self.asked = []

    async def __call__(self, settings, path, params=None, **kw):
        self.asked.append((path, dict(params or {}), kw))
        if self.down:
            raise transport.TmdbError("unreachable")
        if path not in self.answers:
            raise transport.TmdbNotFound(path)
        return self.answers[path]


def answering(**answers):
    tmdb = _Tmdb({path.replace("__", "/"): body for path, body in answers.items()})
    return tmdb, mock.patch.object(transport, "cached_get", tmdb)


class TheDetailsPanelTests(unittest.IsolatedAsyncioTestCase):

    async def test_a_season_of_a_show(self):
        tmdb, patched = answering(tv__95396=SEVERANCE, tv__95396__season__2=SEASON_2)
        with patched:
            got = await detail.fetch_details(SETTINGS, Media.SHOW, "95396", 2)
        self.assertEqual(got["title"], "Severance")
        self.assertEqual(got["certification"], "TV-MA")
        self.assertEqual(got["season_count"], 2)
        self.assertIn("severance barrier", got["season_overview"])
        self.assertEqual([e["number"] for e in got["episodes"]], [1, 2, 3, 10])
        self.assertTrue(got["poster"].startswith("https://wsrv.nl/"))
        self.assertEqual(got["genres"][0], "Drama")
        self.assertEqual(got["ids"]["tvdb"], 371980)
        self.assertEqual(got["videos"][0].sources, (str(Source.TMDB),))
        # One call for the title (everything appended), one for the season.
        self.assertEqual(sorted(path for path, _, _ in tmdb.asked),
                         ["tv/95396", "tv/95396/season/2"])
        appended = {path: params for path, params, _ in tmdb.asked}["tv/95396"]["append_to_response"]
        self.assertEqual(appended, "external_ids,content_ratings,videos")

    async def test_a_film(self):
        _tmdb, patched = answering(movie__693134=DUNE)
        with patched:
            got = await detail.fetch_details(SETTINGS, Media.MOVIE, "693134", None)
        self.assertEqual((got["title"], got["runtime"], got["network"]),
                         ("Dune: Part Two", 167, ""))
        self.assertEqual(got["episodes"], [])

    async def test_a_title_tmdb_does_not_know_answers_the_empty_key_set(self):
        """Not a failure: the panel's handover reads an answer that says nothing
        and asks the next service the card names."""
        _tmdb, patched = answering()
        with patched:
            got = await detail.fetch_details(SETTINGS, Media.SHOW, "1", None)
        self.assertEqual((got["title"], got["overview"], got["episodes"]), ("", "", []))

    async def test_cache_only_is_passed_to_every_read(self):
        tmdb, patched = answering(tv__95396=SEVERANCE, tv__95396__season__2=SEASON_2)
        with patched:
            await detail.fetch_details(SETTINGS, Media.SHOW, "95396", 2, cache_only=True)
        self.assertTrue(all(kw.get("cache_only") for _, _, kw in tmdb.asked))


class TheSeasonSummaryTests(unittest.IsolatedAsyncioTestCase):

    async def test_a_season_tmdb_holds(self):
        _tmdb, patched = answering(tv__95396__season__2=SEASON_2)
        with patched:
            got = await detail.fetch_season_summary(SETTINGS, "95396", 2, Media.SHOW,
                                                    today=date(2026, 10, 22))
        self.assertEqual((got["season"], got["total"]), (2, 4))

    async def test_a_season_tmdb_does_not_hold_is_empty_not_a_failure(self):
        """MEASURED: three seasons on a real roster TMDB does not list are held by
        Simkl; this empty answer is what hands them on."""
        _tmdb, patched = answering()
        with patched:
            got = await detail.fetch_season_summary(SETTINGS, "95396", 17, Media.SHOW)
        self.assertEqual((got["season"], got["total"]), (17, 0))

    async def test_a_tmdb_that_could_not_be_asked_raises(self):
        with mock.patch.object(transport, "cached_get", _Tmdb(down=True)), \
                self.assertRaises(transport.TmdbError):
            await detail.fetch_season_summary(SETTINGS, "95396", 2, Media.SHOW)

    async def test_the_refresh_button_revalidates(self):
        tmdb, patched = answering(tv__95396__season__2=SEASON_2)
        with patched:
            await detail.fetch_season_summary(SETTINGS, "95396", 2, Media.SHOW, fresh=True)
        self.assertEqual(tmdb.asked[0][2]["ttl_seconds"], 0)

    async def test_a_film_has_no_seasons(self):
        tmdb, patched = answering()
        with patched:
            got = await detail.fetch_season_summary(SETTINGS, "693134", 1, Media.MOVIE)
        self.assertEqual(got["total"], 0)
        self.assertEqual(tmdb.asked, [])


class ThePickerFactsAndRecordsTests(unittest.IsolatedAsyncioTestCase):

    async def test_the_season_picker(self):
        _tmdb, patched = answering(tv__95396=SEVERANCE)
        with patched:
            got = await detail.fetch_seasons(SETTINGS, "95396", Media.SHOW)
        self.assertEqual([s["season"] for s in got.seasons], [1, 2])
        self.assertIsNone(got.named_season)
        self.assertEqual(got.network, "Apple TV")

    async def test_the_words_a_tracker_row_is_filed_with(self):
        _tmdb, patched = answering(tv__95396=SEVERANCE)
        with patched:
            facts = await detail.fetch_title_facts(SETTINGS, "95396", Media.SHOW)
        self.assertEqual(tuple(facts), ("Severance", "Apple TV"))

    async def test_records_come_from_the_one_record_builder(self):
        _tmdb, patched = answering(tv__95396=SEVERANCE)
        with patched:
            records = await detail.records_for(SETTINGS, "95396", Media.SHOW,
                                               [(2, "2025-01-16"), (1, "2022-02-17")])
        self.assertEqual([r.episode_label for r in records], ["S02E01", "S01E01"])
        self.assertTrue(all(r.source is Source.TMDB for r in records))


class TheProviderTests(unittest.TestCase):
    """TMDB as the registry holds it: a describer, never a tracker, its search,
    calendar and list ports not yet offered."""

    def test_it_is_first_and_describes_only(self):
        from app import providers
        self.assertEqual(list(providers.registered())[0], Source.TMDB)
        provider = providers.get(Source.TMDB)
        self.assertIsNotNone(provider.detail_port)
        self.assertEqual((provider.sync_port, provider.calendar_port, provider.search_port,
                          provider.watchlist_port), (None, None, None, None))
        self.assertFalse(provider.capabilities.private_user_data)

    def test_its_catalogue_is_the_instance_token_and_nobodys_account(self):
        provider = tmdb_provider._TmdbProvider()
        self.assertTrue(provider.catalogue_is_configured(SETTINGS))
        self.assertFalse(provider.is_configured(SETTINGS))
        self.assertFalse(provider.catalogue_is_configured(Settings()))

    def test_its_pages(self):
        provider = tmdb_provider._TmdbProvider()
        self.assertEqual(provider.title_url(Media.MOVIE, {"tmdb": 5}),
                         "https://www.themoviedb.org/movie/5")
        self.assertEqual(provider.episode_url({"tmdb": 5}, 2, 3),
                         "https://www.themoviedb.org/tv/5/season/2/episode/3")


class TheSearchTests(unittest.IsolatedAsyncioTestCase):
    """search.py is written and held here, and offered to no search box yet."""

    async def test_a_page_of_hits(self):
        tmdb, patched = answering(search__tv=fixture("search_tv_frieren"))
        with patched:
            hits = await search.search_titles(SETTINGS, Media.SHOW, " frieren ")
        self.assertEqual(hits[0].source, Source.TMDB)
        self.assertEqual(hits[0].ids, {"tmdb": int(hits[0].source_id)})
        self.assertIsNone(hits[0].season)
        params = tmdb.asked[0][1]
        self.assertEqual((params["query"], params["include_adult"], params["page"]),
                         ("frieren", "false", "1"))

    async def test_a_page_past_tmdbs_last_is_not_asked_for(self):
        tmdb, patched = answering(search__movie={"results": []})
        with patched:
            await search.search_titles(SETTINGS, Media.MOVIE, "x", page=9999)
        self.assertEqual(tmdb.asked[0][1]["page"], str(search.MAX_PAGE))

    async def test_an_empty_query_asks_nothing(self):
        tmdb, patched = answering()
        with patched:
            self.assertEqual(await search.search_titles(SETTINGS, Media.SHOW, "  "), [])
        self.assertEqual(tmdb.asked, [])

    async def test_a_shared_id_finds_the_tmdb_id_exactly(self):
        _tmdb, patched = answering(**{"find__tt11280740": fixture("find_imdb_severance")})
        with patched:
            got = await search.find_by_shared_id(SETTINGS, Media.SHOW, {"imdb": "tt11280740"})
        self.assertEqual(got, {"tmdb": 95396, "imdb": "tt11280740"})

    async def test_a_film_hit_is_not_taken_for_a_show(self):
        _tmdb, patched = answering(**{"find__tt11280740": fixture("find_imdb_severance")})
        with patched:
            self.assertEqual(await search.find_by_shared_id(
                SETTINGS, Media.MOVIE, {"imdb": "tt11280740"}), {})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
