"""The board key: one spelling for a board row, and one parser for it.

A board row is a title OR one season of a title, and a key that cannot tell them
apart names the wrong row without failing — a removal restored as its whole show,
a save that names one title twice. So this file holds three things:

  - THE ROUND TRIP. Formatting what was parsed gives back the string, for a whole
    show and for a season, which is what lets the data layer compare keys as
    strings. And a key with no season suffix — every key stored before a board
    could hold a season — is the whole show.
  - THE REFUSALS. A film has no seasons; a season is spelled one way.
  - THE GUARD. No module in app/ranker/ reaches the shared ItemKey parser or
    formatter except app/ranker/item_keys.py, because the shared parser does not
    refuse a season key — it reads it as a title with a different id, and a
    lookup by that id matches nothing, silently.
"""
from __future__ import annotations

import ast
import unittest

from app.providers.base import ItemKey, parse_item_key
from app.ranker import item_keys
from app.ranker.item_keys import BoardKey, board_key, parse_board_key
from tests.support import APP_DIR


class RoundTripTests(unittest.TestCase):

    def test_the_whole_show_is_spelled_exactly_as_before(self):
        self.assertEqual(board_key("show", "tmdb", "1234"), "show:tmdb:1234")
        self.assertEqual(board_key("movie", "imdb", "tt0133093"), "movie:imdb:tt0133093")

    def test_a_season_carries_its_suffix(self):
        self.assertEqual(board_key("show", "tmdb", "1234", 2), "show:tmdb:1234:s2")
        self.assertEqual(board_key("show", "tmdb", "1234", 0), "show:tmdb:1234:s0")

    def test_every_key_parses_back_to_itself(self):
        for text in ("show:tmdb:1234", "show:tmdb:1234:s2", "show:tmdb:1234:s0",
                     "show:tvdb:81189:s12", "show:mal:5114:s999", "movie:tmdb:550",
                     "movie:imdb:tt0133093"):
            with self.subTest(text=text):
                self.assertEqual(str(parse_board_key(text)), text)

    def test_an_unsuffixed_key_is_the_whole_show(self):
        """What every stored key — a removal, a deleted tier's list, a backup,
        another tab's page — has always said."""
        parsed = parse_board_key("show:tmdb:1234")
        self.assertIsNone(parsed.season)
        self.assertEqual(parsed.title, ItemKey("show", "tmdb", "1234"))

    def test_a_season_key_names_the_same_title(self):
        parsed = parse_board_key("show:tmdb:1234:s2")
        self.assertEqual((parsed.title, parsed.season), (ItemKey("show", "tmdb", "1234"), 2))
        self.assertEqual(parsed.whole(), BoardKey(ItemKey("show", "tmdb", "1234")))

    def test_a_row_is_keyed_off_its_own_columns(self):
        row = {"media": "show", "match_source": "tmdb", "match_id": "9", "season": 3}
        self.assertEqual(item_keys.row_key(row), "show:tmdb:9:s3")
        self.assertEqual(item_keys.row_key({**row, "season": None}), "show:tmdb:9")

    def test_a_row_without_its_season_column_is_a_bug_not_a_whole_show(self):
        with self.assertRaises(KeyError):
            item_keys.row_key({"media": "show", "match_source": "tmdb", "match_id": "9"})


class RefusalTests(unittest.TestCase):

    def test_a_film_has_no_seasons(self):
        with self.assertRaises(ValueError):
            parse_board_key("movie:tmdb:550:s1")
        with self.assertRaises(ValueError):
            item_keys.valid_season(1, "movie")

    def test_a_season_is_spelled_one_way(self):
        """Only the canonical spelling is a season. Anything else is not read as
        one — `s02` would otherwise format back as `s2` and stop matching the
        string it was parsed from — so it stays part of the id, where the shared
        parser's own rules decide it. None of these names a real title; the point
        is that none of them names a SEASON."""
        for text in ("show:tmdb:1234:s02", "show:tmdb:1234:s-1", "show:tmdb:1234:s1000",
                     "show:tmdb:1234:S2", "show:tmdb:1234:s"):
            with self.subTest(text=text):
                self.assertIsNone(parse_board_key(text).season)

    def test_malformed_keys_are_refused(self):
        for bad in (None, 12, "", "show", "show:tmdb", "show:tmdb:", "tv:tmdb:1",
                    "show:trakt:1", ":s2"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_board_key(bad)

    def test_a_stored_season_is_bounded(self):
        self.assertIsNone(item_keys.valid_season(None, "show"))
        self.assertIsNone(item_keys.valid_season("", "show"))
        self.assertEqual(item_keys.valid_season("4", "show"), 4)
        self.assertEqual(item_keys.valid_season(0, "show"), 0)
        for bad in (-1, 1000, "two", True, 2.5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                item_keys.valid_season(bad, "show")


class TheSharedParserIsNotEnoughTests(unittest.TestCase):
    """Why the guard below exists, measured rather than asserted in prose."""

    def test_the_shared_parser_reads_a_season_as_part_of_the_id(self):
        self.assertEqual(parse_item_key("show:tmdb:1234:s2").match_id, "1234:s2")


class NoRankerModuleUsesTheSharedKeyTests(unittest.TestCase):
    """Every key in app/ranker/ goes through item_keys.py.

    READ WITH THE AST, NOT A TEXT SEARCH, so a docstring that names the shared
    parser to explain why it is not used is not an offence, and an alias
    (`from ..providers.base import parse_item_key as p`) still is — it is the
    imported name that is checked, not what it was called locally.
    """

    OWNER = "item_keys.py"
    FORBIDDEN = frozenset({"parse_item_key", "item_key"})

    def _modules(self):
        return [path for path in sorted((APP_DIR / "ranker").glob("*.py"))
                if path.name != self.OWNER]

    def _offences(self, path) -> list[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                found += [alias.name for alias in node.names if alias.name in self.FORBIDDEN]
            elif isinstance(node, ast.Attribute) and node.attr in self.FORBIDDEN:
                found.append(node.attr)
            elif isinstance(node, ast.Name) and node.id in self.FORBIDDEN:
                found.append(node.id)
        return found

    def test_no_ranker_module_reaches_the_shared_key_functions(self):
        offenders = {path.name: names for path in self._modules()
                     if (names := self._offences(path))}
        self.assertEqual(offenders, {})

    def test_the_guard_is_looking_at_something(self):
        modules = [path.name for path in self._modules()]
        self.assertIn("core.py", modules)
        self.assertIn("routes.py", modules)

    def test_the_guard_would_catch_one(self):
        """The check run against the owner itself, which does import the shared
        parser — so a check that could never fire would fail here."""
        self.assertIn("parse_item_key", self._offences(APP_DIR / "ranker" / self.OWNER))


if __name__ == "__main__":
    unittest.main()
