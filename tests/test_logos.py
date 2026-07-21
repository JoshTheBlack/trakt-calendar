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
