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


if __name__ == "__main__":
    unittest.main()
