"""episode_progress.SeasonProgress — where an episode tick goes.

A tick is pressed to go and correct a count where the count lives, so its link
is the service's own answer (`Provider.episode_url`), asked of the registry. It
used to be an if per service name, with every other service's tick linking
nowhere and an account that synced nothing drawing a Trakt tick regardless.
"""
from __future__ import annotations

import unittest
from unittest import mock

from app import providers
from app.distrakt.episode_progress import SeasonProgress


def _progress(services, by_source=None, ids=None, slugs=None, season=2):
    return SeasonProgress(services, by_source or {}, ids or {}, slugs or {}, season)


class EachTickLinksWhereItsServiceSaysTests(unittest.TestCase):

    def test_trakt_opens_the_episode_by_slug(self):
        tick, = _progress(["trakt"], ids={"trakt": "1390"},
                          slugs={"trakt": "game of thrones"}).ticks(5)
        self.assertEqual(tick.url, "https://app.trakt.tv/shows/game%20of%20thrones"
                                   "?season=2&view=episode&episode=5")

    def test_trakt_falls_back_to_its_id(self):
        tick, = _progress(["trakt"], ids={"trakt": "1390"}).ticks(1)
        self.assertEqual(tick.url, "https://app.trakt.tv/shows/1390"
                                   "?season=2&view=episode&episode=1")

    def test_simkl_opens_the_show(self):
        tick, = _progress(["simkl"], ids={"simkl": "17465"},
                          slugs={"simkl": "game-of-thrones"}).ticks(5)
        self.assertEqual(tick.url, "https://simkl.com/tv/17465/game-of-thrones")

    def test_a_service_with_no_id_links_nowhere(self):
        for name in ("trakt", "simkl"):
            with self.subTest(service=name):
                tick, = _progress([name]).ticks(1)
                self.assertEqual(tick.url, "")

    def test_a_service_registered_later_is_asked_too(self):
        third = mock.Mock(episode_url=mock.Mock(return_value="https://mercury.example/e"))
        real = providers.get
        with mock.patch.object(providers, "get",
                               lambda name: third if name == "mercury" else real(name)):
            tick, = _progress(["mercury"], ids={"mercury": "9"}).ticks(3)
        self.assertEqual(tick.url, "https://mercury.example/e")
        third.episode_url.assert_called_once_with({"mercury": "9", "mercury_slug": ""}, 2, 3)

    def test_a_name_the_registry_does_not_know_raises(self):
        """Every name here came from the tracker's own sources, so an unknown one
        is a bug to see rather than a tick drawn going nowhere."""
        with self.assertRaises(ValueError):
            _progress(["nowhere"]).ticks(1)


class AnAccountSyncingNothingTests(unittest.TestCase):

    def test_a_title_no_tracker_service_knows_draws_no_tick(self):
        self.assertEqual(_progress([], ids={"imdb": "tt1"}).ticks(1), ())

    def test_each_tracker_service_the_title_names_gets_a_linked_tick(self):
        """A tick is a link before it is a claim; the services come from the
        registry and the title's ids, never a default named here."""
        ticks = _progress([], ids={"simkl": "5", "trakt": "9"}).ticks(1)
        expected = [str(s) for s in providers.registered()
                    if str(s) in providers.tracker_sources()]
        self.assertEqual([t.source for t in ticks], expected)
        self.assertTrue(all(t.url for t in ticks))

    def test_a_season_somebody_recorded_still_says_who(self):
        tick, = _progress([], by_source={"simkl": [1]}, ids={"simkl": "5"}).ticks(1)
        self.assertEqual((tick.source, tick.seen), ("simkl", True))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
