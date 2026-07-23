"""Unit tests for the network-logo matching + history-based rollout candidates.

Pure/offline: _pick_network is pure; fetch_watched_progress is tested with
fetch_history mocked. TRAKT_DATA_DIR points at a temp dir before importing.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ["TRAKT_DATA_DIR"] = tempfile.mkdtemp(prefix="distrakt-logos-test-")
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from app import logos, trakt  # noqa: E402


class PickNetworkTests(unittest.TestCase):
    NETS = [{"id": 1, "name": "AMC", "logo_path": "/amc.png"},
            {"id": 2, "name": "Paramount+", "logo_path": "/p.png"},
            {"id": 3, "name": "Netflix", "logo_path": "/n.png"}]

    def test_exact_normalized(self):
        self.assertEqual(logos._pick_network(self.NETS, "Netflix")["id"], 3)

    def test_plus_suffix_matches(self):
        # "AMC+" normalizes to "amc" == "AMC" -> exact.
        self.assertEqual(logos._pick_network(self.NETS, "AMC+")["id"], 1)

    def test_combined_brand_prefix(self):
        # "Paramount+ with Showtime" -> starts with "paramount" -> Paramount+.
        self.assertEqual(logos._pick_network(self.NETS, "Paramount+ with Showtime")["id"], 2)

    def test_no_match_falls_back_to_first(self):
        self.assertEqual(logos._pick_network(self.NETS, "Totally Unknown Net")["id"], 1)

    def test_empty(self):
        self.assertIsNone(logos._pick_network([], "AMC"))


class EnsureLogosTests(unittest.IsolatedAsyncioTestCase):
    """Pre-warm: generate the tiles a roster needs, so a show added before tmdb
    was stored doesn't depend on another show requesting its network first."""

    CFG = SimpleNamespace(tmdb_configured=True)

    async def test_one_lookup_per_network_and_missing_tmdb_skipped(self):
        roster = [
            ("AMC", 11), ("AMC", 12),      # same network twice -> one call
            ("Netflix", 20),
            ("HBO", None), ("", 30),       # no tmdb / no name -> skipped
        ]
        seen = []

        async def fake(settings, network, tmdb):
            seen.append((network, tmdb))
            return logos._tile_path(network)

        with patch("app.logos._tile_path", side_effect=lambda n: __import__("pathlib").Path(f"/nope/{n}.png")), \
             patch("app.logos.is_negative", return_value=False), \
             patch("app.logos.ensure_logo", side_effect=fake):
            generated = await logos.ensure_logos(self.CFG, roster)

        self.assertEqual(sorted(seen), [("AMC", 11), ("Netflix", 20)])
        self.assertEqual(generated, 2)

    async def test_already_cached_or_negative_networks_are_skipped(self):
        async def fake(settings, network, tmdb):
            return logos._tile_path(network)

        # AMC has a tile already; HBO is negative-cached; only Netflix is missing.
        with patch("app.logos._tile_path") as tile, \
             patch("app.logos.is_negative", side_effect=lambda n: n == "HBO"), \
             patch("app.logos.ensure_logo", side_effect=fake) as gen:
            tile.side_effect = lambda n: SimpleNamespace(exists=lambda: n == "AMC")
            await logos.ensure_logos(self.CFG, [("AMC", 1), ("HBO", 2), ("Netflix", 3)])

        called = {c.args[1] for c in gen.call_args_list}
        self.assertEqual(called, {"Netflix"})

    async def test_no_tmdb_key_is_a_noop(self):
        with patch("app.logos.ensure_logo") as gen:
            n = await logos.ensure_logos(SimpleNamespace(tmdb_configured=False), [("AMC", 1)])
        self.assertEqual(n, 0)
        gen.assert_not_called()

    async def test_a_failure_on_one_network_does_not_sink_the_rest(self):
        async def fake(settings, network, tmdb):
            if network == "AMC":
                raise RuntimeError("tmdb down")
            return logos._tile_path(network)

        with patch("app.logos._tile_path", side_effect=lambda n: __import__("pathlib").Path(f"/nope/{n}.png")), \
             patch("app.logos.is_negative", return_value=False), \
             patch("app.logos.ensure_logo", side_effect=fake):
            generated = await logos.ensure_logos(self.CFG, [("AMC", 1), ("Netflix", 2)])
        self.assertEqual(generated, 1)  # Netflix survived


class FetchWatchedProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_aggregates_distinct_episodes_from_history(self):
        events = [
            {"type": "episode", "show": {"title": "A", "ids": {"trakt": 10, "tmdb": 111, "slug": "a"}},
             "episode": {"season": 1, "number": 1}},
            {"type": "episode", "show": {"title": "A", "ids": {"trakt": 10, "tmdb": 111, "slug": "a"}},
             "episode": {"season": 1, "number": 2}},
            {"type": "episode", "show": {"title": "A", "ids": {"trakt": 10, "tmdb": 111, "slug": "a"}},
             "episode": {"season": 1, "number": 2}},  # rewatch -> deduped
            {"type": "movie", "movie": {"title": "M", "ids": {"trakt": 99}}},  # ignored
            {"type": "episode", "show": {"title": "S", "ids": {"trakt": 5}},
             "episode": {"season": 0, "number": 1}},  # special -> skipped
        ]
        with patch("app.trakt.fetch_history", return_value=events):
            out = await trakt.fetch_watched_progress(SimpleNamespace(), since_days=60)
        self.assertEqual(len(out), 1)
        rec = out[0]
        self.assertEqual((rec["trakt_id"], rec["season"], rec["watched"], rec["tmdb"]), (10, 1, 2, 111))


if __name__ == "__main__":
    unittest.main()
