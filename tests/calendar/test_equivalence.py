"""When two services' values for one field are the SAME ANSWER (app/calendar/
resolve.py's `_distinct`): a difference of spelling, or of a word's precision,
offers no flip and no pair of chips — and a rating is always shown beside the
other services'.

MEASURED, which is why this exists: on one October's All Episodes, 1,603 of the
1,610 cards with any such control disagreed about a status, 1,480 of them only
in how each service words one ("airing" beside "returning series"), and dozens
more about a title or a network only in case.

No network, no database.
"""
from __future__ import annotations

import unittest
from zoneinfo import ZoneInfo

from app.calendar import resolve as calendar_resolve
from app.config import Settings
from app.endpoints import get_endpoint
from app.providers.base import Record, Source, render
from app.providers.tmdb import normalize as tmdb_normalize
from app.sources import prefs as source_prefs
from app.templating import templates

from tests.support import stored_groups

AIR_TS = 1784145600.0


def _record(source, **overrides) -> Record:
    name = str(source)
    # TMDB's own id IS the tmdb id; every other service states its own beside it.
    ids = {"tmdb": 900} if source is Source.TMDB else {"tmdb": 900, name: f"{name}-id"}
    values = dict(
        source=source, media="show", id=f"{name}-id", ids=ids,
        detail_url=f"https://{name}.test/show", title="One Title", air_ts=AIR_TS, year=2026,
        network="A Network", country="US", language="EN", runtime=45, status="ended",
        certification="TV-14", overview="One overview.", poster=f"https://{name}.test/p.jpg",
        season=1, episode_number=1, episode_label="S01E01")
    values.update(overrides)
    return Record(**values)


def _resolved(*records: Record, order=()) -> Record:
    groups = stored_groups(list(records))
    assert len(groups) == 1, "these records do not match; the matcher would draw two cards"
    prefs = source_prefs.SourcePrefs(user_id=1, metadata_order=list(order)) if order else None
    return calendar_resolve.resolve(groups[0], prefs)


def _card(*records: Record, order=()) -> str:
    return templates.env.get_template("_card.html").render(
        item=render(_resolved(*records, order=order), ZoneInfo("UTC")), not_watching=set(),
        new_ids=set(), is_admin=False, month=1, year=2026,
        endpoint=get_endpoint("shows/new"), settings=Settings())


class ASpellingIsNotADisagreementTests(unittest.TestCase):

    def test_titles_differing_only_in_case_offer_no_flip(self):
        resolved = _resolved(_record(Source.TRAKT, title="The Show"),
                             _record(Source.SIMKL, title="THE SHOW"))
        self.assertNotIn("title", resolved.alternatives)
        self.assertNotIn('data-field="title"', _card(_record(Source.TRAKT, title="The Show"),
                                                      _record(Source.SIMKL, title="THE SHOW")))

    def test_networks_differing_only_in_spacing_draw_no_chips(self):
        trakt = _record(Source.TRAKT, network="Tokyo MX")
        simkl = _record(Source.SIMKL, network="TOKYO MX ")
        self.assertNotIn("network", _resolved(trakt, simkl).alternatives)
        self.assertNotIn('class="chip dual" data-field="network"', _card(trakt, simkl))

    def test_the_card_names_the_service_whose_spelling_it_shows(self):
        """The two agree, so either is right — but the details panel is opened
        with the service the card is showing, and that is the winner's."""
        trakt, simkl = _record(Source.TRAKT, title="The Show"), _record(Source.SIMKL, title="THE SHOW")
        resolved = _resolved(trakt, simkl, order=("simkl",))
        self.assertEqual(resolved.title, "THE SHOW")
        self.assertEqual(resolved.shown_source("title"), "simkl")
        self.assertEqual(resolved.field_sources["title"], ["simkl", "trakt"])

    def test_a_real_difference_still_flips(self):
        resolved = _resolved(_record(Source.TRAKT, title="The Show"),
                             _record(Source.SIMKL, title="The Show (2026)"))
        self.assertEqual(set(resolved.alternatives["title"]), {"trakt", "simkl"})

    def test_a_runtime_is_whole_minutes(self):
        self.assertNotIn("runtime", _resolved(_record(Source.TRAKT, runtime=45),
                                              _record(Source.SIMKL, runtime=45.0)).alternatives)
        self.assertIn("runtime", _resolved(_record(Source.TRAKT, runtime=45),
                                           _record(Source.SIMKL, runtime=44)).alternatives)

    def test_a_year_is_its_digits(self):
        self.assertNotIn("year", _resolved(_record(Source.TRAKT, year=2024),
                                           _record(Source.SIMKL, year="2024")).alternatives)

    def test_a_picture_is_its_address_exactly(self):
        """A URL's path is case-sensitive; two addresses are two pictures."""
        resolved = _resolved(_record(Source.TRAKT, poster="https://img.test/A.jpg"),
                             _record(Source.SIMKL, poster="https://img.test/a.jpg"))
        self.assertIn("poster", resolved.alternatives)


class OneStatusVocabularyTests(unittest.TestCase):
    """Simkl words a status in three words where Trakt and TMDB use six; each of
    Simkl's covers every word it cannot tell apart (simkl/titles.STATUS_MEANS)."""

    def statuses(self, **by_source) -> dict | None:
        records = [_record(Source(name), status=word) for name, word in by_source.items()]
        return _resolved(*records).alternatives.get("status")

    def test_simkls_word_agrees_with_every_word_it_covers(self):
        for simkl, other in (("airing", "returning series"), ("ended", "ended"),
                             ("ended", "canceled"), ("tba", "in production"),
                             ("tba", "planned"), ("tba", "pilot")):
            with self.subTest(simkl=simkl, other=other):
                self.assertIsNone(self.statuses(simkl=simkl, trakt=other))

    def test_two_services_speaking_the_vocabulary_are_compared_exactly(self):
        self.assertEqual(self.statuses(trakt="ended", tmdb="canceled"),
                         {"tmdb": "canceled", "trakt": "ended"})

    def test_a_word_simkl_does_not_cover_still_disagrees(self):
        self.assertEqual(self.statuses(simkl="airing", trakt="in production"),
                         {"trakt": "in production", "simkl": "airing"})
        self.assertIsNotNone(self.statuses(simkl="tba", trakt="returning series"))

    def test_a_covered_word_steps_aside_for_the_one_that_says_more(self):
        """Three services, two answers: Simkl's 'ended' is TMDB's 'canceled' or
        Trakt's 'ended', said less precisely, so the chips are those two."""
        self.assertEqual(self.statuses(simkl="ended", trakt="ended", tmdb="canceled"),
                         {"tmdb": "canceled", "trakt": "ended"})
        self.assertEqual(self.statuses(simkl="airing", tmdb="returning series",
                                       trakt="in production"),
                         {"tmdb": "returning series", "trakt": "in production"})


class RatingsAreAlwaysSideBySideTests(unittest.TestCase):

    def test_two_services_giving_the_same_score_are_both_shown(self):
        trakt, simkl = _record(Source.TRAKT, rating=8.0), _record(Source.SIMKL, rating=8.0)
        self.assertEqual(_resolved(trakt, simkl).alternatives["rating"],
                         {"trakt": 8.0, "simkl": 8.0})
        self.assertEqual(_card(trakt, simkl).count('<span class="dual-value">⭐ 8.0'), 2)

    def test_one_score_is_no_pair(self):
        self.assertNotIn("rating", _resolved(_record(Source.TRAKT, rating=8.0),
                                             _record(Source.SIMKL)).alternatives)

    def test_tmdbs_score_counts_only_past_its_vote_floor(self):
        """TMDB's one reader drops a score from too few votes, so no stored TMDB
        row — and no card — carries one."""
        floor = tmdb_normalize.RATING_VOTE_FLOOR
        self.assertIsNone(tmdb_normalize.rating({"vote_average": 9.5, "vote_count": floor - 1}))
        self.assertEqual(tmdb_normalize.rating({"vote_average": 7.84, "vote_count": floor}), 7.8)


if __name__ == "__main__":
    unittest.main()
