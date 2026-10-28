"""The six dimensions a card is judged on whole (app/calendar/filter.py's
`keep_title`), and how the read applies them (app/calendar/cache.py's
`narrow_for_viewer`): language, keywords, show type, who lists a card, a
catalogue's undescribed listing with no network, and missing facts.

What these hold is the part that is easy to get wrong silently:
  - each rule's exemption for a card nobody has described — three different
    answers, each one the instance's owner chose with the measurements in front
    of them;
  - that a viewer's SOURCE CHOICE never changes what these judge (the card is
    judged as every service the instance admits describes it);
  - that the operator's floor runs first and a viewer can only narrow what it
    left, uncounted as the viewer's own filtering.

No network.
"""
from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from app import db
from app.calendar import cache as calendar_cache
from app.calendar import filter as calendar_filter
from app.calendar import vocab
from app.config import Settings
from app.endpoints import get_endpoint
from app.providers.base import Media, Record, Source
from app.sources import prefs as source_prefs
from tests.support import migrated_db

SHOWS = get_endpoint("shows/new")
MOVIES = get_endpoint("movies")
AIR_TS = datetime(2026, 10, 7, 12, tzinfo=timezone.utc).timestamp()
SPARSE = frozenset({"tmdb"})


def _record(source=Source.TMDB, slug="t", *, tmdb=100, media="show", **fields) -> Record:
    values = dict(
        source=source, media=media, id=slug, ids={str(source): slug, "tmdb": tmdb},
        detail_url=f"https://example.test/{slug}", title=slug.title(), air_ts=AIR_TS,
        country="US", language="EN", network="HBO", poster="https://img.test/p.jpg",
    )
    if media == "show":
        values.update(season=1, episode_number=1, episode_label="S01E01")
    values.update(fields)
    return Record(**values)


def _kept(record, listed_by=("tmdb",), **specs) -> bool:
    rules = calendar_filter.TitleRules.parse(**specs)
    return calendar_filter.keep_title(record, frozenset(listed_by), rules, SPARSE)


class EachRuleTests(unittest.TestCase):

    def test_language_includes_and_excludes_like_a_country(self):
        english, japanese = _record(language="EN"), _record(language="JA")
        self.assertTrue(_kept(english, languages="en"))
        self.assertFalse(_kept(japanese, languages="en"))
        self.assertFalse(_kept(japanese, languages="-ja"))
        self.assertTrue(_kept(english, languages="-ja"))

    def test_a_title_with_no_language_is_dropped_by_an_include_and_kept_by_an_exclude(self):
        silent = _record(language="")
        self.assertFalse(_kept(silent, languages="en"))
        self.assertTrue(_kept(silent, languages="-ja"))

    def test_keywords_match_any_named_keyword_folded(self):
        anime = _record(keywords=["anime", "based on manga"])
        self.assertFalse(_kept(anime, keywords=["-Anime"]))
        self.assertTrue(_kept(anime, keywords=["based on manga"]))
        self.assertFalse(_kept(_record(keywords=["office"]), keywords=["based on manga"]))

    def test_a_keyword_holding_a_comma_is_one_keyword(self):
        sf = _record(keywords=["san francisco, california"])
        self.assertFalse(_kept(sf, keywords=["-san francisco, california"]))
        self.assertTrue(_kept(sf, keywords=["-san francisco"]))

    def test_stated_no_keywords_is_judged_and_unstated_is_not(self):
        """An empty list from a description is TMDB's answer; None is no answer."""
        self.assertFalse(_kept(_record(keywords=[]), keywords=["anime"]))
        self.assertTrue(_kept(_record(keywords=[]), keywords=["-anime"]))
        self.assertTrue(_kept(_record(keywords=None), keywords=["anime"]))

    def test_show_type_chips(self):
        talk = _record(show_type="talk-show")
        self.assertFalse(_kept(talk, show_types="-talk-show"))
        self.assertTrue(_kept(talk, show_types="-news"))
        self.assertFalse(_kept(talk, show_types="scripted"))
        self.assertTrue(_kept(_record(show_type=None), show_types="scripted"))

    def test_single_service_counts_who_lists_it(self):
        self.assertFalse(_kept(_record(), ("tmdb",), single_service=True))
        self.assertTrue(_kept(_record(), ("tmdb", "trakt"), single_service=True))
        self.assertTrue(_kept(_record(), ("tmdb",)))

    def test_sparse_no_network_hides_only_a_catalogue_only_card_with_no_network(self):
        nameless = _record(network="")
        self.assertFalse(_kept(nameless, ("tmdb",), sparse_no_network=True))
        self.assertTrue(_kept(_record(network="HBO"), ("tmdb",), sparse_no_network=True))
        self.assertTrue(_kept(nameless, ("tmdb", "trakt"), sparse_no_network=True),
                        "another service lists it")
        self.assertTrue(_kept(_record(Source.SIMKL, network=""), ("simkl",),
                              sparse_no_network=True), "Simkl does not list sparse")

    def test_missing_rules_hide_a_card_missing_every_fact_a_rule_names(self):
        nothing = _record(country="", language="", poster="")
        no_country = _record(country="")
        self.assertFalse(_kept(no_country, missing="country"))
        self.assertTrue(_kept(no_country, missing="country+poster"))
        self.assertFalse(_kept(nothing, missing="country+poster"))
        self.assertFalse(_kept(_record(poster=""), missing="language, poster"))
        self.assertTrue(_kept(_record(), missing="country, language, poster"))

    def test_an_unknown_missing_rule_does_nothing(self):
        self.assertTrue(_kept(_record(country=""), missing="genre"))


class WhatAnUndescribedCardIsJudgedOnTests(unittest.TestCase):
    """The three answers, side by side."""

    def undescribed(self, **fields):
        return _record(enriched=False, country="", language="", network="",
                       keywords=None, show_type=None, **fields)

    def test_language_and_missing_data_show_it_unjudged(self):
        card = self.undescribed()
        self.assertTrue(_kept(card, languages="en"))
        self.assertTrue(_kept(card, missing="country, language"))

    def test_keywords_and_show_type_wait_for_the_catalogue_whoever_else_described_it(self):
        """A Trakt record is enriched and states neither fact: the card is still
        shown unjudged by both."""
        trakt = _record(Source.TRAKT, keywords=None, show_type=None)
        self.assertTrue(_kept(trakt, ("trakt",), keywords=["anime"], show_types="scripted"))

    def test_who_lists_it_is_judged_from_the_start(self):
        card = self.undescribed()
        self.assertFalse(_kept(card, ("tmdb",), single_service=True))
        self.assertFalse(_kept(card, ("tmdb",), sparse_no_network=True),
                         "the owner's direction: an undescribed catalogue listing is hidden")


class TheReadTests(unittest.IsolatedAsyncioTestCase):
    """narrow_for_viewer, through visible_records, over real groups."""

    async def asyncSetUp(self):
        migrated_db("titlefilters")

    async def asyncTearDown(self):
        db.close_thread_connection()

    def groups(self):
        return calendar_cache.group_records([
            # Both services list it; only TMDB states its keywords.
            _record(Source.TMDB, "1", tmdb=1, title="Shared Anime", keywords=["anime"],
                    show_type="scripted"),
            _record(Source.TRAKT, "shared-anime", tmdb=1, title="Shared Anime"),
            # Only TMDB lists it, and it names no network.
            _record(Source.TMDB, "2", tmdb=2, title="Bare Listing", network="",
                    keywords=[], show_type="reality"),
            # Only Trakt lists it.
            _record(Source.TRAKT, "trakt-alone", tmdb=3, title="Trakt Alone"),
        ])

    async def read(self, selection="auto", settings=None, **specs):
        prefs = source_prefs.SourcePrefs(user_id=1, calendar_source=selection)
        kept, filtered = await calendar_cache.visible_records(
            self.groups(), SHOWS, prefs=prefs, settings=settings, **specs)
        return sorted(r.title for r in kept), filtered

    async def test_a_source_choice_never_changes_who_lists_a_card(self):
        """Narrowed to Trakt, "Shared Anime" is drawn from Trakt alone — and is
        still a card two services list, so the single-service filter keeps it."""
        for selection in ("auto", "trakt"):
            with self.subTest(source=selection):
                titles, _ = await self.read(selection, single_service=True)
                self.assertIn("Shared Anime", titles)
                self.assertNotIn("Trakt Alone", titles)

    async def test_a_source_choice_never_changes_what_the_keywords_judge(self):
        for selection in ("auto", "trakt"):
            with self.subTest(source=selection):
                titles, filtered = await self.read(selection, keywords=["-anime"])
                self.assertNotIn("Shared Anime", titles)
                self.assertEqual(filtered, 1)

    async def test_the_no_network_rule(self):
        titles, _ = await self.read(sparse_no_network=True)
        self.assertEqual(titles, ["Shared Anime", "Trakt Alone"])

    async def test_a_floor_a_viewer_cannot_loosen_and_does_not_count(self):
        floor = Settings(tv_single_service=True)
        titles, filtered = await self.read(settings=floor)
        self.assertEqual(titles, ["Shared Anime"])
        self.assertEqual(filtered, 0, "the operator's floor is not this viewer's filtering")
        titles, filtered = await self.read(settings=floor, show_types="-scripted")
        self.assertEqual(titles, [])
        self.assertEqual(filtered, 1)

    async def test_a_film_floor_does_not_reach_a_show_calendar(self):
        titles, _ = await self.read(settings=Settings(movie_single_service=True,
                                                      movie_missing="poster"))
        self.assertEqual(titles, ["Bare Listing", "Shared Anime", "Trakt Alone"])

    async def test_the_language_and_keyword_floors_are_one_for_both_media(self):
        settings = Settings(languages="-en", keywords=["-anime"])
        for media in (Media.SHOW, Media.MOVIE):
            with self.subTest(media=media):
                floor = vocab.floor_specs(settings, media)
                self.assertEqual((floor["languages"], floor["keywords"]), ("-en", ["-anime"]))


class TheFillsLanguageFloorTests(unittest.IsolatedAsyncioTestCase):
    """The one new floor a listing can be judged on by itself, applied at fill as
    the country floor is — and with no exemption, as that one has none."""

    async def asyncSetUp(self):
        migrated_db("languagefloor")

    async def asyncTearDown(self):
        db.close_thread_connection()

    async def test_a_floored_language_never_reaches_storage(self):
        class Port:
            async def fetch_window(self, endpoint, settings, start, days, *, revalidate=False):
                return [_record(Source.TRAKT, "english", tmdb=1, language="EN"),
                        _record(Source.TRAKT, "japanese", tmdb=2, language="JA")]

        class Provider:
            source = Source.TRAKT
            calendar_port = Port()

        records, answered, _unchanged, _pending = await calendar_cache._answers(
            [Provider()], SHOWS, Settings(languages="-ja"), date(2026, 10, 5))
        self.assertEqual([r.id for r in records], ["english"])
        self.assertEqual(answered, ["trakt"])


class ExemptionCountTests(unittest.TestCase):
    """exempts_undescribed: which cards are on a page only by exemption."""

    def test_the_new_dimensions_exempt_as_their_rules_do(self):
        none = calendar_cache.exempts_undescribed(Media.SHOW, Settings())
        self.assertFalse(none)
        by_language = calendar_cache.exempts_undescribed(Media.SHOW, Settings(), languages="en")
        self.assertTrue(by_language.lets_through(_record(enriched=False)))
        self.assertFalse(by_language.lets_through(_record(keywords=None)))
        by_keyword = calendar_cache.exempts_undescribed(Media.SHOW, Settings(), keywords=["anime"])
        self.assertTrue(by_keyword.lets_through(_record(keywords=None)))
        self.assertFalse(by_keyword.lets_through(_record(keywords=[])))
        by_floor_type = calendar_cache.exempts_undescribed(
            Media.SHOW, Settings(tv_show_types="scripted"))
        self.assertTrue(by_floor_type.lets_through(_record(show_type=None)))

    def test_who_lists_a_card_exempts_nothing(self):
        self.assertFalse(calendar_cache.exempts_undescribed(
            Media.SHOW, Settings(tv_single_service=True), sparse_no_network=True))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
