"""How a rendered share card is addressed and how long it is kept.

Neither question needs a database, a client or a renderer: the key is a hash of
a value object and the sweep is a directory walk, so this file stands alone.
"""
from __future__ import annotations

import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.calendar import share_card, share_card_cache


def _card(**overrides) -> share_card.Card:
    fields = {"month_label": "August", "year": 2026, "count": 12, "tiles": (), "avatar": None}
    return share_card.Card(**{**fields, **overrides})


def _tile(title: str, media: str = "show", tmdb: int = 55, *,
          date_label: str = "Fri 14 Aug", is_premiere: bool = False) -> share_card.Tile:
    return share_card.Tile(title=title, poster=Path("/data/posters") / media / f"{tmdb}.jpg",
                           date_label=date_label, is_premiere=is_premiere)


class RenderKeyTests(unittest.TestCase):
    def _key(self, card, owner_id=1, month=8) -> str:
        return share_card_cache.render_key(card, owner_id=owner_id, month=month)

    def test_the_same_picture_gets_the_same_address(self):
        self.assertEqual(self._key(_card()), self._key(_card()))

    def test_a_month_that_gained_a_title_renders_anew(self):
        """The key IS the invalidation: a different line-up is a different
        address, with no bookkeeping anywhere to forget a case."""
        one = _card(tiles=(_tile("First"),))
        two = _card(tiles=(_tile("First"), _tile("Second", tmdb=56)))
        self.assertNotEqual(self._key(one), self._key(two))

    def test_every_drawn_input_moves_the_address(self):
        for changed in (_card(count=13), _card(year=2027), _card(month_label="September"),
                        _card(tiles=(_tile("Only"),)), _card(avatar=b"pretend-an-image")):
            with self.subTest(card=changed):
                self.assertNotEqual(self._key(_card()), self._key(changed))
        self.assertNotEqual(self._key(_card()), self._key(_card(), owner_id=2))
        self.assertNotEqual(self._key(_card()), self._key(_card(), month=9))

    def test_a_tile_whose_date_or_premiere_mark_changed_renders_anew(self):
        """Both are DRAWN — the date under the title, the glow round the poster
        — so both are inputs, and an input the key omits is one a viewer can
        watch change while being handed back the old picture forever."""
        plain = _card(tiles=(_tile("Show"),))
        for changed in (_card(tiles=(_tile("Show", date_label="Sat 15 Aug"),)),
                        _card(tiles=(_tile("Show", is_premiere=True),))):
            with self.subTest(card=changed):
                self.assertNotEqual(self._key(plain), self._key(changed))

    def test_the_same_title_backed_by_a_different_poster_renders_anew(self):
        self.assertNotEqual(self._key(_card(tiles=(_tile("Show", tmdb=1),))),
                            self._key(_card(tiles=(_tile("Show", tmdb=2),))))

    def test_a_renderer_version_bump_renders_anew(self):
        """Otherwise a layout change keeps serving yesterday's geometry to
        everyone who unfurled a link before it."""
        before = self._key(_card())
        with patch.object(share_card, "RENDERER_VERSION", share_card.RENDERER_VERSION + 1):
            self.assertNotEqual(before, self._key(_card()))

    def test_the_key_is_only_ever_hex(self):
        """It is the one thing that becomes a path component, so nothing a
        stranger can type ever reaches the filesystem."""
        key = self._key(_card(tiles=(_tile("../../etc/passwd\x00"),)))
        self.assertRegex(key, r"^[0-9a-f]{32}$")
        self.assertEqual(share_card_cache.cache_path(key).parent,
                         share_card_cache.CACHE_DIR)


class SweepTests(unittest.TestCase):
    def setUp(self):
        share_card_cache.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        for stale in share_card_cache.CACHE_DIR.iterdir():
            stale.unlink()

    def _write(self, name: str, size: int, *, age_days: float = 0.0) -> Path:
        path = share_card_cache.CACHE_DIR / name
        path.write_bytes(b"x" * size)
        when = time.time() - age_days * 24 * 60 * 60
        os.utime(path, (when, when))
        return path

    def _names(self) -> set[str]:
        return {p.name for p in share_card_cache.CACHE_DIR.iterdir()}

    def test_a_card_past_the_retention_window_goes(self):
        fresh = self._write("fresh.jpg", 10)
        self._write("old.jpg", 10, age_days=share_card_cache.RETENTION_DAYS + 1)
        self.assertEqual(share_card_cache.sweep(10 ** 9), 1)
        self.assertEqual(self._names(), {fresh.name})

    def test_the_budget_evicts_oldest_first(self):
        self._write("oldest.jpg", 100, age_days=30)
        self._write("middle.jpg", 100, age_days=20)
        newest = self._write("newest.jpg", 100, age_days=1)
        self.assertEqual(share_card_cache.sweep(150), 2)
        self.assertEqual(self._names(), {newest.name})

    def test_a_directory_inside_the_budget_is_left_alone(self):
        self._write("a.jpg", 100)
        self._write("b.jpg", 100)
        self.assertEqual(share_card_cache.sweep(10 ** 9), 0)
        self.assertEqual(len(self._names()), 2)


if __name__ == "__main__":
    unittest.main()
