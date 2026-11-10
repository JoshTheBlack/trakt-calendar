"""Unit tests for the network-logo matching + history-based rollout candidates.

Pure/offline: _pick_network is pure; fetch_watched_progress is tested with
fetch_history mocked.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

from app.media import logos
from app.providers.trakt import sync as trakt_sync


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

    def test_a_show_on_another_network_names_none(self):
        """A show TMDB files under a different network is no evidence about this
        one: the old fallback to its first network drew BBC Two with Channel 4's
        logo."""
        self.assertIsNone(logos._pick_network(self.NETS, "Totally Unknown Net"))

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
        with patch("app.providers.trakt.sync.fetch_history", return_value=events):
            out = await trakt_sync.fetch_watched_progress(SimpleNamespace(), since_days=60)
        self.assertEqual(len(out), 1)
        rec = out[0]
        self.assertEqual((rec["season"], rec["watched"]), (1, 2))
        # The whole id map travels, because the caller decides which of them the
        # row it writes is keyed on.
        # `trakt_slug` beside `slug`: Trakt's normalizer namespaces the slug it
        # was handed, because Simkl calls that field `slug` too and disagrees on
        # what it holds.
        self.assertEqual(rec["ids"], {"trakt": 10, "tmdb": 111, "slug": "a",
                                      "trakt_slug": "a"})


class ANameWithNoLatinLettersTests(unittest.TestCase):

    def test_a_name_with_no_latin_letters_gets_its_own_file(self):
        """Every such name used to slug to "" — one shared ".png" and ".none"."""
        names = ["哔哩哔哩", "НТВ", "テレビ東京"]
        slugs = [logos.slug(n) for n in names]
        self.assertTrue(all(slugs))
        self.assertEqual(len(set(slugs)), 3)
        self.assertEqual(logos.slug("哔哩哔哩"), slugs[0])  # stable
        self.assertEqual(logos.slug("AMC+"), "amc")          # Latin names unchanged
