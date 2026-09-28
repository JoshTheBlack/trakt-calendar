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


if __name__ == "__main__":
    unittest.main()
