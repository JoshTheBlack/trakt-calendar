"""app/providers/tmdb/detail.py and search.py, answered from TMDB's real payloads.

`transport.cached_get` is replaced by a table of paths to saved answers, so what
is held is what each verb asks for and how it reads the answer — including the
two answers that matter most once TMDB is first: a season TMDB does not hold is
an EMPTY season (so the next service answers it), and a TMDB that could not be
asked RAISES (so a tracker row keeps its last-known numbers).
"""
from __future__ import annotations

import dataclasses
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
        tmdb, patched = answering(tv__95396=SEVERANCE, tv__95396__season__2=SEASON_2,
                                  tv__95396__season__2__aggregate_credits=fixture(
                                      "cast_tv_severance_season_2"))
        with patched:
            got = await detail.fetch_details(SETTINGS, Media.SHOW, "95396", 2)
        self.assertEqual(got["title"], "Severance")
        self.assertEqual(got["cast"][0]["name"], "Adam Scott")
        self.assertEqual(got["certification"], "TV-MA")
        self.assertEqual(got["season_count"], 2)
        self.assertIn("severance barrier", got["season_overview"])
        self.assertEqual([e["number"] for e in got["episodes"]], [1, 2, 3, 10])
        # TMDB's origin address, which the title store keeps; a page proxies it.
        self.assertTrue(got["poster"].startswith("https://image.tmdb.org/"))
        self.assertEqual(got["genres"][0], "Drama")
        self.assertEqual(got["ids"]["tvdb"], 371980)
        self.assertEqual(got["videos"][0].sources, (str(Source.TMDB),))
        # One call for the title (everything appended), one for the season, and
        # the season's own cast beside them.
        self.assertEqual(sorted(path for path, _, _ in tmdb.asked),
                         ["tv/95396", "tv/95396/season/2",
                          "tv/95396/season/2/aggregate_credits"])
        asked = {path: params for path, params, _ in tmdb.asked}
        self.assertEqual(asked["tv/95396"], {
            "append_to_response": "external_ids,content_ratings,videos,keywords,images",
            # The logo's language is the instance's, and no other.
            "include_image_language": "en"})
        self.assertEqual(asked["tv/95396/season/2"], {"append_to_response": "videos"})
        # The two facts only TMDB states, from that same one call.
        self.assertEqual(got["keywords"][:2], ["office", "workplace"])
        self.assertEqual(got["show_type"], "scripted")

    async def test_the_extras_ride_on_the_same_two_calls(self):
        show = {**SEVERANCE, "images": fixture("images_tv_severance"),
                "next_episode_to_air": fixture("next_episode_tv")["next_episode_to_air"]}
        season = {**SEASON_2, "videos": fixture("season_videos_tv")["videos"]}
        _tmdb, patched = answering(tv__95396=show, tv__95396__season__2=season)
        with patched:
            got = await detail.fetch_details(SETTINGS, Media.SHOW, "95396", 2)
        self.assertTrue(got["logo"].endswith("/yyS1tALk7t3YdTNMOvR5gsnXINA.png"))
        self.assertEqual(got["next_episode"]["first_aired"],
                         show["next_episode_to_air"]["air_date"])
        self.assertEqual(got["collection"], {})
        self.assertTrue(got["season_videos"])
        self.assertNotEqual([v.key for v in got["season_videos"]],
                            [v.key for v in got["videos"]])

    async def test_the_image_language_follows_the_instances(self):
        tmdb, patched = answering(tv__95396=SEVERANCE)
        with patched:
            await detail.fetch_details(dataclasses.replace(SETTINGS, tmdb_language="de-DE"),
                                       Media.SHOW, "95396", None)
        asked = {path: params for path, params, _ in tmdb.asked}
        self.assertEqual(asked["tv/95396"]["include_image_language"], "de")


    async def test_an_answer_asked_without_keywords_states_neither_fact(self):
        """An answer that carries no keyword list — one cached before they were
        asked for — says nothing about keywords or type, which is not the same
        as "none": the store then reads the title as undescribed for both."""
        bare = {k: v for k, v in SEVERANCE.items() if k != "keywords"}
        _tmdb, patched = answering(tv__95396=bare)
        with patched:
            got = await detail.fetch_details(SETTINGS, Media.SHOW, "95396", None)
        self.assertIsNone(got["keywords"])
        self.assertEqual(got["show_type"], "scripted")

    async def test_a_whole_show_draws_the_shows_cast(self):
        _tmdb, patched = answering(tv__95396=SEVERANCE,
                                   tv__95396__aggregate_credits=fixture("cast_tv_severance"))
        with patched:
            got = await detail.fetch_details(SETTINGS, Media.SHOW, "95396", None)
        self.assertEqual([c["name"] for c in got["cast"]][:1], ["Adam Scott"])

    async def test_a_season_with_no_cast_listed_draws_the_shows(self):
        tmdb, patched = answering(tv__95396=SEVERANCE, tv__95396__season__2=SEASON_2,
                                  tv__95396__aggregate_credits=fixture("cast_tv_severance"))
        with patched:
            got = await detail.fetch_details(SETTINGS, Media.SHOW, "95396", 2)
        self.assertEqual(got["cast"][0]["name"], "Adam Scott")
        self.assertIn("tv/95396/aggregate_credits", [path for path, _, _ in tmdb.asked])

    async def test_a_film(self):
        _tmdb, patched = answering(movie__693134=DUNE,
                                   movie__693134__credits=fixture("cast_movie_dune_part_two"))
        with patched:
            got = await detail.fetch_details(SETTINGS, Media.MOVIE, "693134", None)
        self.assertTrue(got["cast"])
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


class TheCollectionTests(unittest.IsolatedAsyncioTestCase):

    async def test_a_collections_films_in_the_order_they_came_out(self):
        tmdb, patched = answering(collection__726871=fixture("collection_dune"))
        with patched:
            parts = await detail.fetch_collection(SETTINGS, 726871)
        self.assertEqual([part.title for part in parts],
                         ["Dune", "Dune: Part Two", "Dune: Part Three"])
        self.assertEqual(parts[0], detail.CollectionPart(438631, "Dune", 2021))
        self.assertEqual([path for path, _, _ in tmdb.asked], ["collection/726871"])

    async def test_an_undated_film_goes_last(self):
        body = {"parts": [{"id": 2, "title": "Later", "release_date": ""},
                          {"id": 1, "title": "First", "release_date": "2001-01-01"}]}
        _tmdb, patched = answering(collection__9=body)
        with patched:
            parts = await detail.fetch_collection(SETTINGS, 9)
        self.assertEqual([(p.tmdb_id, p.year) for p in parts], [(1, 2001), (2, "")])

    async def test_a_collection_tmdb_does_not_hold_is_empty(self):
        _tmdb, patched = answering()
        with patched:
            self.assertEqual(await detail.fetch_collection(SETTINGS, 1), [])

    async def test_a_tmdb_that_cannot_be_asked_raises(self):
        with mock.patch.object(transport, "cached_get", _Tmdb(down=True)):
            with self.assertRaises(transport.TmdbError):
                await detail.fetch_collection(SETTINGS, 726871)

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
        self.assertEqual((facts.title, facts.network, facts.network_from),
                         ("Severance", "Apple TV", ""))

    async def test_records_come_from_the_one_record_builder(self):
        _tmdb, patched = answering(tv__95396=SEVERANCE)
        with patched:
            records = await detail.records_for(SETTINGS, "95396", Media.SHOW,
                                               [(2, "2025-01-16"), (1, "2022-02-17")])
        self.assertEqual([r.episode_label for r in records], ["S02E01", "S01E01"])
        self.assertTrue(all(r.source is Source.TMDB for r in records))


class TheProviderTests(unittest.TestCase):
    """TMDB as the registry holds it: a describer, every box's default search and
    a calendar of three premieres, never a tracker, its list port not yet
    offered."""

    def test_it_is_first_and_describes_searches_and_lists_premieres(self):
        from app import providers
        self.assertEqual(list(providers.registered())[0], Source.TMDB)
        provider = providers.get(Source.TMDB)
        self.assertIsNotNone(provider.detail_port)
        self.assertIsNotNone(provider.search_port)
        self.assertIsNotNone(provider.calendar_port)
        self.assertTrue(provider.capabilities.searched_by_default)
        self.assertEqual(provider.capabilities.endpoints,
                         {"shows/new", "shows/premieres", "movies"})
        self.assertTrue(provider.capabilities.describes_sparse_listings)
        # A linked viewer's TMDB watchlist is written through its list port; it
        # keeps no watch history, so it has no sync port and never backs the tracker.
        self.assertIsNone(provider.sync_port)
        self.assertIsNotNone(provider.watchlist_port)
        self.assertFalse(provider.capabilities.private_user_data)

    def test_its_calendar_is_the_instance_token(self):
        provider = tmdb_provider._TmdbProvider()
        self.assertTrue(provider.calendar_port.calendar_configured(SETTINGS))
        self.assertFalse(provider.calendar_port.calendar_configured(Settings()))

    def test_its_catalogue_is_the_instance_token_and_nobodys_account(self):
        provider = tmdb_provider._TmdbProvider()
        self.assertTrue(provider.catalogue_is_configured(SETTINGS))
        self.assertFalse(provider.is_configured(SETTINGS))
        self.assertFalse(provider.catalogue_is_configured(Settings()))

    def test_its_private_question_is_a_viewers_own_grant(self):
        """`is_configured` is true only where a Settings carries a viewer's TMDB
        token AND the account it names — what auth.viewer.settings_for_user
        swaps in for somebody who linked TMDB — never on the instance's read
        token, and never on half a grant."""
        provider = tmdb_provider._TmdbProvider()
        linked = dataclasses.replace(SETTINGS, tmdb_user_token="a-user-token",
                                     tmdb_account_id="6a5edaa7df66e2fc7c922663")
        self.assertTrue(provider.is_configured(linked))
        self.assertFalse(provider.is_configured(dataclasses.replace(linked, tmdb_account_id="")))
        self.assertFalse(provider.is_configured(dataclasses.replace(linked, tmdb_user_token="")))

    def test_its_pages(self):
        provider = tmdb_provider._TmdbProvider()
        self.assertEqual(provider.title_url(Media.MOVIE, {"tmdb": 5}),
                         "https://www.themoviedb.org/movie/5")
        self.assertEqual(provider.episode_url({"tmdb": 5}, 2, 3),
                         "https://www.themoviedb.org/tv/5/season/2/episode/3")


class TheSearchTests(unittest.IsolatedAsyncioTestCase):
    """search.py: one page per ask, and TMDB's own word on whether there is more."""

    async def test_a_page_of_hits(self):
        tmdb, patched = answering(search__tv=fixture("search_tv_frieren"))
        with patched:
            answer = await search.search_titles(SETTINGS, Media.SHOW, " frieren ")
        hits = answer.hits
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
            self.assertEqual((await search.search_titles(SETTINGS, Media.SHOW, "  ")).hits, [])
        self.assertEqual(tmdb.asked, [])

    async def test_more_is_tmdbs_page_count_capped_where_tmdb_stops(self):
        cases = [(1, 3, True), (3, 3, False), (1, 1, False), (499, 900, True),
                 (500, 900, False)]
        for page, total, more in cases:
            with self.subTest(page=page, total=total):
                _tmdb, patched = answering(search__tv={"results": [], "total_pages": total})
                with patched:
                    answer = await search.search_titles(SETTINGS, Media.SHOW, "x", page=page)
                self.assertEqual(answer.more, more)

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
