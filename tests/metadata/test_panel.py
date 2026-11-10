"""app/metadata/panel.py — what a details panel says, from the title store.

THE PROMISES:
  - THE DESCRIBING SERVICE IS ASKED ONCE, and its answer kept: a second panel on
    the same title — on any surface — is drawn from the store with no request,
    and a stale stored answer is still drawn (the surface queues its refresh).
  - A STORED ANSWER IS THE SAME ANSWER as a fresh one: the DetailPort key set, the
    same values, read back through one function.
  - EVERY SERVICE'S VIDEOS ARE POOLED, each asked by its own id, a failure
    contributing nothing; a season a service lacks goes to the next that holds it.
  - A PAGE DRAWS PICTURES THROUGH THE PROXY and days in the instance's zone,
    whichever service answered (`presented`).
"""
from __future__ import annotations

import asyncio
import json
from datetime import date
from unittest import mock
from zoneinfo import ZoneInfo

from app import db, providers
from app.config import Settings
from app.metadata import descriptions, panel
from app.providers import detail_source
from app.providers.base import Source, Video, VideoKind
from tests.support import DatabaseTestCase, season_groups, title_groups


def _video(video_id, source, kind="trailer"):
    return Video(site="youtube", video_id=video_id,
                 url=f"https://www.youtube.com/watch?v={video_id}", title="",
                 kind=VideoKind(kind), official=None, published=None,
                 sources=(source,))


def _answer(source: str, own_id: str, season, *, videos=(), episodes=True, **fields) -> dict:
    """A service's answer in DetailPort's key set, naming its own id."""
    return {
        "title": "T", "year": 2024, "overview": "o", "season_overview": "", "status": "Ended",
        "network": "", "country": "", "language": "", "anime_type": "",
        "release_types_by_country": {}, "ids": {source: int(own_id)},
        "poster": f"https://{source}.example/p.jpg", "runtime": 50, "genres": ["Drama"],
        "genre_slugs": ["drama"], "rating": 7.5, "imdb_rating": None, "certification": "",
        "videos": list(videos), "first_aired": "", "homepage": "", "season": season,
        "season_title": "", "season_count": 2, "cast": [], "logo": "", "collection": {},
        "next_episode": None, "season_videos": [],
        "episodes": ([{"number": 1, "title": "One", "first_aired": "2026-07-15T02:00:00Z",
                       "rating": None, "overview": "", "episode_type": "", "runtime": None}]
                     if episodes and season is not None else []),
        **fields}


class _DetailPort:
    """One service's detail port: a description carrying its own videos, and
    the same list on its own — or a failure, where the test says so."""

    def __init__(self, source, videos, *, configured=True, episodes=True, fails=False,
                 seasons=()):
        self.source, self.videos = source, videos
        self.configured, self.episodes, self.fails = configured, episodes, fails
        self.seasons = list(seasons)
        self.asked = []
        # Season lists asked for, apart from `asked`: what the panel's strip reads.
        self.listed = []

    def catalogue_configured(self, settings):
        return self.configured

    async def fetch_details(self, settings, media, source_id, season, *, cache_only=False,
                            fresh=False):
        self.asked.append(("details", source_id, cache_only))
        return _answer(self.source, source_id, season, videos=self.videos,
                       episodes=self.episodes)

    async def fetch_videos(self, settings, media, source_id, *, cache_only=False):
        from app.providers.base import SourceUnavailable
        self.asked.append(("videos", source_id, cache_only))
        if self.fails:
            raise SourceUnavailable("down")
        return list(self.videos)

    async def fetch_seasons(self, settings, source_id, media, *, cached_only=False, fresh=False):
        from app.providers.base import SeasonsAnswer, SourceUnavailable
        self.listed.append((source_id, cached_only))
        if self.fails:
            raise SourceUnavailable("down")
        return SeasonsAnswer(seasons=[{"season": n, "episode_count": 8} for n in self.seasons],
                             named_season=None, ids={}, network="")


class _Registry:
    """Fake detail ports installed where the panel and detail_source ask."""

    def __init__(self, ports: dict):
        self.ports = ports

    def __enter__(self):
        # Each service's own answer for its words (Provider.episode_mark), which
        # a panel's episodes are presented with.
        real = providers.get
        self._patches = [
            mock.patch.object(detail_source, "ports",
                              lambda: [(s, p) for s, p in self.ports.items()]),
            mock.patch.object(providers, "get",
                              lambda source: mock.Mock(
                                  detail_port=self.ports[Source(source)],
                                  episode_mark=real(Source(source)).episode_mark)),
        ]
        for patch in self._patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in self._patches:
            patch.stop()


class APanelsSeasonStripIsWhatItsServicesListTests(DatabaseTestCase):
    """describe_panel's `seasons`: every season a service the ids name lists —
    never a count up to the season open, which for a show Simkl numbers by year
    was 2,026 tabs."""

    async def panel(self, ids, *, trakt=(), simkl=(), simkl_fails=False, with_seasons=True,
                    media="show"):
        ports = {Source.TRAKT: _DetailPort("trakt", [], seasons=trakt),
                 Source.SIMKL: _DetailPort("simkl", [], seasons=simkl, fails=simkl_fails)}
        chosen = detail_source.Answerer(Source.TRAKT, ids.get("trakt", ""), stored_only=False)
        with _Registry(ports):
            _who, details = await panel.describe_panel(
                Settings(), chosen, ids, media, 2026 if media == "show" else None,
                with_seasons=with_seasons)
        return details, ports

    async def test_the_strip_is_every_season_any_service_lists(self):
        """Season 5 only Simkl lists, 2026 the one the answer describes."""
        details, _ports = await self.panel({"trakt": "1", "simkl": "2"},
                                           trakt=[1, 2], simkl=[1, 5])
        self.assertEqual([entry["season"] for entry in details["seasons"]], [1, 2, 5, 2026])

    async def test_a_service_that_cannot_list_them_adds_nothing(self):
        details, _ports = await self.panel({"trakt": "1", "simkl": "2"}, trakt=[1, 2],
                                           simkl=[1, 5], simkl_fails=True)
        self.assertEqual([entry["season"] for entry in details["seasons"]], [1, 2, 2026])

    async def test_a_surface_drawing_no_strip_asks_for_no_list(self):
        """The answer keeps its own list — here the one season it describes — and
        no service is asked for another."""
        details, ports = await self.panel({"trakt": "1", "simkl": "2"}, trakt=[1],
                                          with_seasons=False)
        self.assertEqual([entry["season"] for entry in details["seasons"]], [2026])
        self.assertEqual(ports[Source.TRAKT].listed + ports[Source.SIMKL].listed, [])

    async def test_a_film_has_no_strip_to_list(self):
        details, ports = await self.panel({"trakt": "1"}, media="movie")
        self.assertEqual(details.get("seasons", []), [], "no other service's list was read")
        self.assertEqual(ports[Source.TRAKT].listed, [])


class APanelPoolsEveryServicesVideosTests(DatabaseTestCase):
    """describe_panel: the description is the chosen service's, and its
    `videos` are every service's the ids name, pooled."""

    async def panel(self, ids, *, trakt=None, simkl=None, season=None, stored_only=False,
                    cache_only=False):
        ports = {Source.TRAKT: trakt or _DetailPort("trakt", [_video("traktvideo1", "trakt")]),
                 Source.SIMKL: simkl or _DetailPort("simkl", [_video("simklvideo1", "simkl",
                                                                     "teaser")])}
        chosen = detail_source.Answerer(Source.TRAKT, ids.get("trakt", ""),
                                        stored_only=stored_only)
        with _Registry(ports):
            who, details = await panel.describe_panel(
                Settings(), chosen, ids, "show", season, cache_only=cache_only)
        return who, details, ports

    async def test_both_services_videos_are_on_the_panel_with_the_best_first(self):
        _who, details, ports = await self.panel({"trakt": "1", "simkl": "2"})
        self.assertEqual([v.video_id for v in details["videos"]],
                         ["traktvideo1", "simklvideo1"])
        self.assertEqual(details["title"], "T")
        # The other service is asked for its list alone, by ITS OWN id.
        self.assertEqual(ports[Source.SIMKL].asked, [("videos", "2", False)])

    async def test_a_service_the_ids_do_not_name_is_not_asked(self):
        """What bounds the share page: a map with only one service's id asks
        only that service."""
        _who, details, ports = await self.panel({"trakt": "1"})
        self.assertEqual([v.video_id for v in details["videos"]], ["traktvideo1"])
        self.assertEqual(ports[Source.SIMKL].asked, [])

    async def test_a_service_that_fails_contributes_nothing_and_fails_nothing(self):
        simkl = _DetailPort("simkl", [_video("simklvideo1", "simkl")], fails=True)
        _who, details, _ports = await self.panel({"trakt": "1", "simkl": "2"}, simkl=simkl)
        self.assertEqual([v.video_id for v in details["videos"]], ["traktvideo1"])

    async def test_cache_only_reaches_the_other_service_too(self):
        _who, _details, ports = await self.panel({"trakt": "1", "simkl": "2"}, cache_only=True)
        self.assertEqual(ports[Source.SIMKL].asked, [("videos", "2", True)])

    async def test_a_service_this_instance_cannot_reach_is_read_from_its_cache(self):
        simkl = _DetailPort("simkl", [], configured=False)
        _who, _details, ports = await self.panel({"trakt": "1", "simkl": "2"}, simkl=simkl)
        self.assertEqual(ports[Source.SIMKL].asked, [("videos", "2", True)])

    async def test_another_services_stored_videos_are_read_from_the_store(self):
        """A service whose description the store already holds costs no request
        for its list."""
        await descriptions.store(Source.SIMKL, "show", "2",
                                 _answer("simkl", "2", None,
                                         videos=[_video("simklstored", "simkl")]),
                                 asked_season=None, tz=ZoneInfo("UTC"))
        _who, details, ports = await self.panel({"trakt": "1", "simkl": "2"})
        self.assertIn("simklstored", [v.video_id for v in details["videos"]])
        self.assertEqual(ports[Source.SIMKL].asked, [])

    async def test_a_season_handed_to_a_later_service_keeps_the_first_services_videos(self):
        """The season rule can move the description to Simkl; the title's videos
        are still every service's, the first one's included."""
        trakt = _DetailPort("trakt", [_video("traktvideo1", "trakt")], episodes=False)
        who, details, ports = await self.panel({"trakt": "1", "simkl": "2"}, trakt=trakt,
                                               season=4)
        self.assertEqual(who.source, Source.SIMKL)
        self.assertEqual({v.video_id for v in details["videos"]},
                         {"traktvideo1", "simklvideo1"})
        # The describer's own list now leads its kind — Simkl's teaser still
        # follows Trakt's trailer, because a trailer outranks a teaser.
        self.assertEqual(details["videos"][0].video_id, "traktvideo1")
        # Trakt's own description was stored, so its list costs nothing more.
        self.assertNotIn(("videos", "1", False), ports[Source.TRAKT].asked)

    async def test_a_description_that_fails_leaves_no_other_services_read_running(self):
        """The panel fails as it always did; the other services' reads it started
        beside the description stop with it rather than asking on for nobody."""
        from app.providers.base import SourceUnavailable

        trakt = _DetailPort("trakt", [])
        trakt.fetch_details = mock.AsyncMock(side_effect=SourceUnavailable("down"))
        simkl = _DetailPort("simkl", [])
        answered = []

        async def slow_videos(settings, media, source_id, *, cache_only=False):
            await asyncio.sleep(0.05)
            answered.append(source_id)
            return []

        simkl.fetch_videos = slow_videos
        with self.assertRaises(SourceUnavailable):
            await self.panel({"trakt": "1", "simkl": "2"}, trakt=trakt, simkl=simkl)
        await asyncio.sleep(0.3)
        self.assertEqual(answered, [], "Simkl's read went on after the panel had failed")

    async def test_nothing_to_describe_is_still_nothing(self):
        trakt = _DetailPort("trakt", [])
        trakt.fetch_details = mock.AsyncMock(return_value={"videos": []})
        _who, details, _ports = await self.panel({"trakt": "1", "simkl": "2"}, trakt=trakt,
                                                 stored_only=True)
        self.assertIsNone(details)


class TheStoreAnswersTests(DatabaseTestCase):
    """The describing service is asked once; after that the store answers."""

    async def describe(self, port, season=1, *, cache_only=False):
        chosen = detail_source.Answerer(Source.TRAKT, "7", stored_only=False)
        with _Registry({Source.TRAKT: port}):
            return await panel.describe(Settings(), chosen, "show", season, ids={"trakt": "7"},
                                        cache_only=cache_only)

    async def test_a_second_panel_is_drawn_from_the_store_without_a_request(self):
        port = _DetailPort("trakt", [])
        first = await self.describe(port)
        second = await self.describe(port)
        self.assertEqual(len(port.asked), 1)
        self.assertEqual(first, second)

    async def test_a_stored_answer_has_the_detail_ports_key_set(self):
        port = _DetailPort("trakt", [_video("v1", "trakt")])
        answer = await self.describe(port)
        self.assertEqual(set(answer), set(descriptions.ANSWER_KEYS))
        self.assertEqual(answer["videos"][0].video_id, "v1")
        self.assertEqual(answer["genres"], ["Drama"])
        self.assertEqual(answer["status"], "Ended")
        self.assertEqual(answer["rating"], 7.5)
        self.assertEqual([e["title"] for e in answer["episodes"]], ["One"])

    async def test_a_stale_stored_answer_is_still_drawn(self):
        port = _DetailPort("trakt", [])
        await self.describe(port)
        await db.execute("UPDATE titles SET trakt_stale_after = 0")
        await db.execute("UPDATE seasons SET trakt_stale_after = 0")
        await self.describe(port)
        self.assertEqual(len(port.asked), 1)

    async def test_a_season_not_yet_described_is_asked_for(self):
        port = _DetailPort("trakt", [])
        await self.describe(port, season=1)
        await self.describe(port, season=2)
        self.assertEqual([own_id for _what, own_id, _cache in port.asked], ["7", "7"])
        self.assertEqual(len(await season_groups("trakt")), 2)

    async def test_a_cache_only_read_is_not_kept(self):
        """A share page's read of a service's response cache may be partial;
        written as a description it would mark the title described."""
        port = _DetailPort("trakt", [])
        await self.describe(port, cache_only=True)
        self.assertEqual(await title_groups(), [])


class PresentedTests(DatabaseTestCase):
    def test_pictures_go_through_the_proxy_and_days_are_written_in_the_instance_zone(self):
        settings = Settings(timezone="America/New_York")
        shown = panel.presented({
            "poster": "https://trakt.example/p.jpg",
            "cast": [{"name": "A", "character": "B", "headshot": "https://t.example/h.jpg"},
                     {"name": "C", "character": "", "headshot": None}],
            "episodes": [{"number": 1, "first_aired": "2026-07-15T02:00:00Z"},
                         {"number": 2, "first_aired": "2026-07-22"},
                         {"number": 3, "first_aired": ""}],
        }, settings, Source.TRAKT)
        self.assertTrue(shown["poster"].startswith("https://wsrv.nl/?url="))
        self.assertTrue(shown["cast"][0]["headshot"].startswith("https://wsrv.nl/?url="))
        self.assertIsNone(shown["cast"][1]["headshot"])
        # 02:00Z on the 15th is the evening of the 14th in New York; a bare day is
        # that day everywhere; an undated episode has none.
        self.assertEqual([e["air_display"] for e in shown["episodes"]],
                         ["14 Jul 2026", "22 Jul 2026", ""])


class AWholeTitleGoesToWhoCanDescribeItTests(DatabaseTestCase):
    """describe_season with NO season: an answer that says nothing about the
    title hands it to the next service the card names. With TMDB first, a card
    whose tmdb id TMDB does not know would otherwise open blank on a title the
    next service describes — and a share page, reading cache_only, keeps drawing
    what the service that described a title before TMDB led had stored."""

    IDS = {"tmdb": "1", "trakt": "7"}
    EMPTY = {"title": "", "overview": "", "episodes": []}

    async def _describe(self, answers, *, cache_only=False):
        asked = []

        def port_for(source):
            async def fetch_details(settings, media, source_id, season, *, cache_only=False):
                asked.append((str(source), cache_only))
                return answers[str(source)]
            return mock.Mock(detail_port=mock.Mock(
                catalogue_configured=lambda settings: True, fetch_details=fetch_details))

        ports = [(Source.TMDB, None), (Source.TRAKT, None)]
        chosen = detail_source.Answerer(Source.TMDB, "1", stored_only=False)
        with mock.patch.object(detail_source, "ports", lambda: ports), \
                mock.patch.object(providers, "get", port_for):
            who, answer = await panel.describe_season(
                Settings(), chosen, self.IDS, "show", None, cache_only=cache_only)
        return str(who.source), answer, asked

    async def test_an_answer_that_says_nothing_goes_to_the_next_service(self):
        who, answer, asked = await self._describe({"tmdb": self.EMPTY,
                                                   "trakt": {**self.EMPTY, "title": "Silo"}})
        self.assertEqual((who, answer["title"]), ("trakt", "Silo"))
        self.assertEqual([s for s, _ in asked], ["tmdb", "trakt"])

    async def test_an_answer_that_describes_the_title_stands(self):
        who, _answer, asked = await self._describe({"tmdb": {**self.EMPTY, "title": "Silo"},
                                                    "trakt": {**self.EMPTY, "title": "Other"}})
        self.assertEqual((who, [s for s, _ in asked]), ("tmdb", ["tmdb"]))

    async def test_a_share_page_reads_the_next_services_cache(self):
        who, _answer, asked = await self._describe(
            {"tmdb": self.EMPTY, "trakt": {**self.EMPTY, "overview": "Stored."}},
            cache_only=True)
        self.assertEqual(who, "trakt")
        self.assertTrue(all(cache_only for _, cache_only in asked))

    async def test_an_answer_that_says_nothing_is_not_stored(self):
        await self._describe({"tmdb": self.EMPTY, "trakt": {**self.EMPTY, "title": "Silo"}})
        stored = {row["source"]: json.loads(row["ids_json"]) for row in await title_groups()}
        self.assertNotIn("tmdb", stored)


class TheExtrasArePresentedTests(DatabaseTestCase):
    """`presented`: the logo through the proxy, the next episode only while it is
    ahead, and each episode's mark by its own service's word."""

    def test_the_logo_goes_through_the_proxy_as_a_png(self):
        shown = panel.presented({"logo": "https://image.tmdb.org/t/p/w500/a.svg"},
                                Settings(), Source.TMDB)
        self.assertTrue(shown["logo"].startswith("https://wsrv.nl/?url="))
        self.assertIn("output=png", shown["logo"])
        self.assertEqual(panel.presented({"logo": ""}, Settings(), Source.TMDB)["logo"], "")

    def test_the_next_episode_is_drawn_while_it_is_ahead_and_never_after(self):
        def next_on(day):
            return panel.presented({"next_episode": {
                "season": 2, "number": 5, "title": "Five", "first_aired": day,
                "episode_type": "finale"}}, Settings(timezone="UTC"), Source.TMDB)["next_episode"]

        with mock.patch.object(panel.clock, "today", return_value=date(2026, 11, 8)):
            today, later, before, undated = (next_on("2026-11-08"), next_on("2026-11-20"),
                                             next_on("2026-11-07"), next_on(""))
        self.assertEqual((today["air_display"], today["mark_label"]), ("08 Nov 2026", "Finale"))
        self.assertEqual(later["number"], 5)
        self.assertIsNone(before, "a date in the past is never drawn as next")
        self.assertEqual(undated["air_display"], "")
        self.assertIsNone(panel.presented({"next_episode": None}, Settings(),
                                          Source.TMDB)["next_episode"])

    def test_each_episode_is_marked_by_its_own_services_word(self):
        def marks(source, words):
            shown = panel.presented({"episodes": [
                {"number": n, "first_aired": "", "episode_type": word}
                for n, word in enumerate(words, 1)]}, Settings(), source)
            return [e["mark_label"] for e in shown["episodes"]]

        self.assertEqual(marks(Source.TMDB, ["standard", "mid_season", "finale"]),
                         ["", "Mid-season finale", "Finale"])
        self.assertEqual(marks(Source.TRAKT, ["season_premiere", "mid_season_finale",
                                              "season_finale", "series_finale"]),
                         ["", "Mid-season finale", "Finale", "Finale"])
        # One service's word is never read as another's.
        self.assertEqual(marks(Source.TRAKT, ["finale", "mid_season"]), ["", ""])
        self.assertEqual(marks(Source.TMDB, ["season_finale"]), [""])


class ASeasonsOwnVideosLeadTheMenuTests(DatabaseTestCase):
    """Opened on a season, the panel's menu leads with that season's own videos,
    then every service's for the title."""

    async def test_the_seasons_own_trailer_plays_first(self):
        season_trailer = _video("seasontrailer", "tmdb")

        class _WithSeasonVideos(_DetailPort):
            async def fetch_details(self, settings, media, source_id, season, *,
                                    cache_only=False, fresh=False):
                return {**await super().fetch_details(settings, media, source_id, season),
                        "season_videos": [season_trailer]}

        # TMDB is the service that lists a season's own videos, and the one whose
        # group keeps them (its store columns).
        ports = {Source.TMDB: _WithSeasonVideos("tmdb", [_video("titletrailer", "tmdb")]),
                 Source.SIMKL: _DetailPort("simkl", [_video("simklteaser", "simkl", "teaser")])}
        chosen = detail_source.Answerer(Source.TMDB, "1", stored_only=False)
        with _Registry(ports):
            _who, details = await panel.describe_panel(Settings(), chosen,
                                                       {"tmdb": "1", "simkl": "2"}, "show", 1)
        self.assertEqual([v.video_id for v in details["videos"]],
                         ["seasontrailer", "titletrailer", "simklteaser"])
