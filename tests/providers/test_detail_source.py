"""detail_source.seasons: who lists a title's seasons is who describes it.

The rankings board's season control asks through this rather than reaching a
provider itself, so "which service answers for this title" is one question for a
season list as it already is for a description. What is held here is the one
thing the verb adds over calling the port: a stored-only answerer — a source this
instance cannot reach right now — is asked of its store alone, never over the
network.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from app import providers
from app.config import Settings
from app.providers import detail_source
from app.providers.base import Media, SeasonsAnswer, Source


class _Port:
    def __init__(self):
        self.calls = []

    async def fetch_seasons(self, settings, source_id, media, *, cached_only=False):
        self.calls.append((source_id, media, cached_only))
        return SeasonsAnswer(seasons=[{"season": 1, "episode_count": 8, "first_aired": ""}],
                             named_season=None, ids={}, network="")


class SeasonsAsksTheChosenSourceTests(unittest.TestCase):

    def ask(self, stored_only: bool):
        port = _Port()
        provider = mock.Mock(detail_port=port)
        chosen = detail_source.Answerer(Source.SIMKL, "3252881", stored_only=stored_only)
        with mock.patch.object(providers, "get", lambda source: provider):
            answer = asyncio.run(detail_source.seasons(Settings(), chosen, "show"))
        return port.calls, answer

    def test_a_reachable_source_is_asked(self):
        calls, answer = self.ask(stored_only=False)
        self.assertEqual(calls, [("3252881", Media.SHOW, False)])
        self.assertEqual(answer.seasons[0]["season"], 1)

    def test_a_source_that_cannot_be_reached_is_asked_of_its_store_only(self):
        calls, _ = self.ask(stored_only=True)
        self.assertEqual(calls, [("3252881", Media.SHOW, True)])


class ASeasonGoesToWhoHoldsItTests(unittest.TestCase):
    """for_season: the chosen service answers, unless it holds nothing of that
    season and a later service the title names does. MEASURED: Simkl files the
    anthology "Monster" under the ids of Trakt's "DAHMER – Monster", which has
    one season, so Trakt answered season 4 with nothing while Simkl held eight
    dated episodes."""

    IDS = {"trakt": "167797", "simkl": "1446614"}

    def run_for(self, answers: dict, *, stored_only=False, simkl_configured=True):
        asked = []

        async def ask(answerer):
            asked.append(str(answerer.source))
            got = answers[str(answerer.source)]
            if isinstance(got, Exception):
                raise got
            return got

        ports = [(Source.TRAKT, None), (Source.SIMKL, None)]
        configured = {Source.TRAKT: True, Source.SIMKL: simkl_configured}

        def get(source):
            return mock.Mock(detail_port=mock.Mock(
                catalogue_configured=lambda settings: configured[Source(source)]))

        chosen = detail_source.Answerer(Source.TRAKT, "167797", stored_only=stored_only)
        with mock.patch.object(detail_source, "ports", lambda: ports), \
                mock.patch.object(providers, "get", get):
            who, answer = asyncio.run(detail_source.for_season(
                Settings(), self.IDS, chosen, ask, lambda got: got["total"] > 0))
        return str(who.source), answer, asked

    def test_the_chosen_service_answers_when_it_holds_the_season(self):
        self.assertEqual(self.run_for({"trakt": {"total": 10}, "simkl": {"total": 8}}),
                         ("trakt", {"total": 10}, ["trakt"]))

    def test_a_season_the_chosen_service_lacks_goes_to_one_that_holds_it(self):
        self.assertEqual(self.run_for({"trakt": {"total": 0}, "simkl": {"total": 8}}),
                         ("simkl", {"total": 8}, ["trakt", "simkl"]))

    def test_with_nobody_holding_it_the_first_answer_stands(self):
        self.assertEqual(self.run_for({"trakt": {"total": 0}, "simkl": {"total": 0}})[:2],
                         ("trakt", {"total": 0}))

    def test_a_later_service_that_fails_is_skipped(self):
        from app.providers.base import SourceUnavailable
        who, answer, _ = self.run_for({"trakt": {"total": 0},
                                       "simkl": SourceUnavailable("down")})
        self.assertEqual((who, answer), ("trakt", {"total": 0}))

    def test_a_failure_of_the_chosen_service_is_not_an_empty_season(self):
        from app.providers.base import SourceUnavailable
        with self.assertRaises(SourceUnavailable):
            self.run_for({"trakt": SourceUnavailable("down"), "simkl": {"total": 8}})

    def test_a_service_this_instance_cannot_ask_is_not_asked(self):
        _, _, asked = self.run_for({"trakt": {"total": 0}, "simkl": {"total": 8}},
                                   simkl_configured=False)
        self.assertEqual(asked, ["trakt"])

    def test_a_stored_only_answer_is_never_followed(self):
        _, _, asked = self.run_for({"trakt": {"total": 0}, "simkl": {"total": 8}},
                                   stored_only=True)
        self.assertEqual(asked, ["trakt"])


def _video(video_id, source, kind="trailer"):
    from app.providers.base import Video, VideoKind
    return Video(site="youtube", video_id=video_id,
                 url=f"https://www.youtube.com/watch?v={video_id}", title="",
                 kind=VideoKind(kind), official=None, published=None,
                 sources=(source,))


class _DetailPort:
    """One service's detail port: a description carrying its own videos, and
    the same list on its own — or a failure, where the test says so."""

    def __init__(self, source, videos, *, configured=True, episodes=True, fails=False):
        self.source, self.videos = source, videos
        self.configured, self.episodes, self.fails = configured, episodes, fails
        self.asked = []

    def catalogue_configured(self, settings):
        return self.configured

    async def fetch_details(self, settings, media, source_id, season, *, cache_only=False):
        self.asked.append(("details", source_id, cache_only))
        return {"title": "T", "overview": "o", "videos": list(self.videos),
                "episodes": [{"number": 1}] if self.episodes else []}

    async def fetch_videos(self, settings, media, source_id, *, cache_only=False):
        from app.providers.base import SourceUnavailable
        self.asked.append(("videos", source_id, cache_only))
        if self.fails:
            raise SourceUnavailable("down")
        return list(self.videos)


class APanelPoolsEveryServicesVideosTests(unittest.TestCase):
    """describe_panel: the description is the chosen service's, and its
    `videos` are every service's the ids name, pooled."""

    def panel(self, ids, *, trakt=None, simkl=None, season=None, stored_only=False,
              cache_only=False):
        ports = {Source.TRAKT: trakt or _DetailPort("trakt", [_video("traktvideo1", "trakt")]),
                 Source.SIMKL: simkl or _DetailPort("simkl", [_video("simklvideo1", "simkl",
                                                                     "teaser")])}
        chosen = detail_source.Answerer(Source.TRAKT, ids.get("trakt", ""),
                                        stored_only=stored_only)
        with mock.patch.object(detail_source, "ports",
                               lambda: [(s, p) for s, p in ports.items()]), \
                mock.patch.object(providers, "get",
                                  lambda source: mock.Mock(detail_port=ports[Source(source)])):
            who, details = asyncio.run(detail_source.describe_panel(
                Settings(), chosen, ids, "show", season, cache_only=cache_only))
        return who, details, ports

    def test_both_services_videos_are_on_the_panel_with_the_best_first(self):
        _who, details, ports = self.panel({"trakt": "1", "simkl": "2"})
        self.assertEqual([v.video_id for v in details["videos"]],
                         ["traktvideo1", "simklvideo1"])
        self.assertEqual(details["title"], "T")
        # The other service is asked for its list alone, by ITS OWN id.
        self.assertEqual(ports[Source.SIMKL].asked, [("videos", "2", False)])

    def test_a_service_the_ids_do_not_name_is_not_asked(self):
        """What bounds the share page: a map with only one service's id asks
        only that service."""
        _who, details, ports = self.panel({"trakt": "1"})
        self.assertEqual([v.video_id for v in details["videos"]], ["traktvideo1"])
        self.assertEqual(ports[Source.SIMKL].asked, [])

    def test_a_service_that_fails_contributes_nothing_and_fails_nothing(self):
        simkl = _DetailPort("simkl", [_video("simklvideo1", "simkl")], fails=True)
        _who, details, _ports = self.panel({"trakt": "1", "simkl": "2"}, simkl=simkl)
        self.assertEqual([v.video_id for v in details["videos"]], ["traktvideo1"])

    def test_cache_only_reaches_the_other_service_too(self):
        _who, _details, ports = self.panel({"trakt": "1", "simkl": "2"}, cache_only=True)
        self.assertEqual(ports[Source.SIMKL].asked, [("videos", "2", True)])

    def test_a_service_this_instance_cannot_reach_is_read_from_its_store(self):
        simkl = _DetailPort("simkl", [], configured=False)
        _who, _details, ports = self.panel({"trakt": "1", "simkl": "2"}, simkl=simkl)
        self.assertEqual(ports[Source.SIMKL].asked, [("videos", "2", True)])

    def test_a_season_handed_to_a_later_service_keeps_the_first_services_videos(self):
        """The season rule can move the description to Simkl; the title's videos
        are still every service's, the first one's included."""
        trakt = _DetailPort("trakt", [_video("traktvideo1", "trakt")], episodes=False)
        who, details, ports = self.panel({"trakt": "1", "simkl": "2"}, trakt=trakt, season=4)
        self.assertEqual(who.source, Source.SIMKL)
        self.assertEqual({v.video_id for v in details["videos"]},
                         {"traktvideo1", "simklvideo1"})
        # The describer's own list now leads its kind — Simkl's teaser still
        # follows Trakt's trailer, because a trailer outranks a teaser.
        self.assertEqual(details["videos"][0].video_id, "traktvideo1")
        self.assertIn(("videos", "1", False), ports[Source.TRAKT].asked)

    def test_nothing_to_describe_is_still_nothing(self):
        trakt = _DetailPort("trakt", [])
        trakt.fetch_details = mock.AsyncMock(return_value={"videos": []})
        who, details, _ports = self.panel({"trakt": "1", "simkl": "2"}, trakt=trakt,
                                          stored_only=True)
        self.assertIsNone(details)


if __name__ == "__main__":
    unittest.main()


class OutboundLinksAskEveryRegisteredServiceTests(unittest.TestCase):
    """outbound_links reaches each service's own `title_url` through the
    registry. It used to hold a table of two packages' builders, so a service
    registered later got no button and nothing said so."""

    def test_a_service_registered_later_gets_its_button(self):
        third = mock.Mock(title_url=lambda media, ids: f"https://mercury.example/{ids['mercury']}")
        registry = {**providers.registered(), "mercury": third}
        with mock.patch.object(providers, "registered", lambda: registry):
            links = detail_source.outbound_links({"mercury": "7", "trakt": "1"}, "show")
        self.assertIn(detail_source.Outbound("mercury", "https://mercury.example/7"), links)

    def test_only_services_the_title_names_are_asked(self):
        third = mock.Mock(title_url=mock.Mock(return_value="https://mercury.example/x"))
        registry = {**providers.registered(), "mercury": third}
        with mock.patch.object(providers, "registered", lambda: registry):
            links = detail_source.outbound_links({"trakt": "1"}, "show")
        third.title_url.assert_not_called()
        self.assertEqual([str(link.source) for link in links], ["trakt"])

    def test_links_follow_the_declared_order(self):
        links = detail_source.outbound_links({"simkl": "2", "trakt": "1"}, "movie")
        self.assertEqual([str(link.source) for link in links],
                         [str(s) for s in providers.registered()
                          if str(s) in ("simkl", "trakt")])
