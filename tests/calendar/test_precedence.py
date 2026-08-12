"""Which source's value a viewer sees for each field of a merged card.

The exclusion half of resolution — which sources a viewer reads AT ALL — is
pinned in test_resolve.py and is deliberately not restated here. This file is
about what happens among the survivors: one card, several services describing
it, and one account's answer for each field of it.

THE CLAIM THE WHOLE DESIGN RESTS ON has its own class at the end. Resolution runs
at READ over rows filled without knowing who would read them, so changing a
preference must invalidate nothing and refetch nothing — and that is asserted by
forbidding the fetch outright rather than by observing that none happened to
occur.

No network, and no database except where a class says otherwise.
"""
from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app import db
from app.calendar import cache as calendar_cache, resolve as calendar_resolve
from app.config import Settings
from app.endpoints import get_endpoint
from app.providers.base import Record, Source
from app.sources import prefs as source_prefs
from tests.support import new_db_path

SHOWS = get_endpoint("shows")
AIR_TS = 1784145600.0


def _record(source, **overrides) -> Record:
    """One source's record for one airing, with everything a card draws."""
    name = str(source)
    values = dict(
        source=source, media="show", id=f"{name}-id",
        ids={"tmdb": 900, name: f"{name}-id"},
        detail_url=f"https://{name}.test/show", title=f"{name.title()} Title",
        air_ts=AIR_TS, year=2026, network=f"{name.title()} Network",
        country="us", language="en", runtime=45, status="returning series",
        rating=8.1, genres=["drama"], certification="TV-14",
        overview=f"{name.title()} overview.", poster=f"https://{name}.test/p.jpg",
        season=1, episode_number=1, episode_label="S01E01",
        episode_title="Pilot",
    )
    values.update(overrides)
    return Record(**values)


def _group(*records: Record) -> dict:
    """The stored group shape, built the way a fill builds it — so every group
    below is one the matcher would really produce."""
    groups = calendar_cache.group_records(list(records))
    assert len(groups) == 1, "these records do not match; the matcher would draw two cards"
    return groups[0]


def _forced_group(*records: Record) -> dict:
    """The same shape, assembled without asking the matcher.

    For the cases where the SHAPE is what is under test and the matcher would
    (correctly) refuse to merge the records — two different seasons of one title
    is two airings, and the matcher exists to keep them apart. Resolution still
    has to have an answer for a group holding both, because that is what a group
    holding two coordinates looks like from here.
    """
    group = {"key": "forced", "ids": {}, "by_source": {}}
    for record in records:
        group["by_source"][str(record.source)] = record.to_dict()
        for namespace, value in (record.ids or {}).items():
            group["ids"].setdefault(namespace, value)
    return group


def _prefs(**precedence) -> source_prefs.SourcePrefs:
    return source_prefs.SourcePrefs(user_id=1, precedence=precedence)


class TheDefaultIsTheDeclaredOrderTests(unittest.TestCase):
    """An account that has stated nothing gets the declared order for every
    field, which is what the app has always shown. There is no seeded table of
    per-field defaults to drift out of step with it."""

    def setUp(self):
        self.group = _group(_record(Source.TRAKT), _record(Source.SIMKL))

    def test_every_field_comes_from_the_first_declared_source(self):
        record = calendar_resolve.resolve(self.group)
        self.assertEqual(str(record.source), "trakt")
        self.assertEqual(record.title, "Trakt Title")
        self.assertEqual(record.overview, "Trakt overview.")
        self.assertEqual(record.poster, "https://trakt.test/p.jpg")
        self.assertEqual(record.network, "Trakt Network")
        self.assertEqual(record.detail_url, "https://trakt.test/show")

    def test_stating_nothing_and_nobody_asking_are_the_same_answer(self):
        stated = calendar_resolve.resolve(self.group, _prefs())
        anonymous = calendar_resolve.resolve(self.group, None)
        self.assertEqual(stated.title, anonymous.title)
        self.assertEqual(str(stated.source), str(anonymous.source))

    def test_a_field_only_the_other_source_carries_still_arrives(self):
        """A preference REORDERS and never excludes, so the winning source having
        nothing to say does not empty the card."""
        group = _group(_record(Source.TRAKT, certification="", runtime=None),
                       _record(Source.SIMKL, certification="TV-MA", runtime=24))
        record = calendar_resolve.resolve(group)
        self.assertEqual(str(record.source), "trakt")
        self.assertEqual(record.certification, "TV-MA")
        self.assertEqual(record.runtime, 24)


class AFieldOverrideMovesOneFieldTests(unittest.TestCase):
    """The per-field half of the model: naming a source for one field changes
    that field and leaves every other one exactly where it was."""

    def setUp(self):
        self.group = _group(_record(Source.TRAKT), _record(Source.SIMKL))
        self.baseline = calendar_resolve.resolve(self.group)

    def resolved(self, **precedence):
        return calendar_resolve.resolve(self.group, _prefs(**precedence))

    def test_naming_a_source_for_one_field_moves_exactly_that_field(self):
        record = self.resolved(fields={"poster": "simkl"})
        self.assertEqual(record.poster, "https://simkl.test/p.jpg")
        for field_name in ("title", "overview", "network", "rating",
                           "certification", "status", "language", "country"):
            with self.subTest(field=field_name):
                self.assertEqual(getattr(record, field_name),
                                 getattr(self.baseline, field_name))
        # ...including the card's own identity, which is a field of its own.
        self.assertEqual(str(record.source), "trakt")
        self.assertEqual(record.detail_url, "https://trakt.test/show")

    def test_the_account_default_moves_every_field_that_has_no_override(self):
        record = self.resolved(default="simkl")
        self.assertEqual(record.title, "Simkl Title")
        self.assertEqual(record.overview, "Simkl overview.")
        self.assertEqual(str(record.source), "simkl")

    def test_a_field_override_beats_the_account_default(self):
        record = self.resolved(default="simkl", fields={"title": "trakt"})
        self.assertEqual(record.title, "Trakt Title")
        self.assertEqual(record.overview, "Simkl overview.")

    def test_two_accounts_read_one_group_and_get_different_answers(self):
        """The same stored group, two opposite preferences, no copying and no
        second row anywhere."""
        first = calendar_resolve.resolve(self.group, _prefs(default="trakt"))
        second = calendar_resolve.resolve(self.group, _prefs(default="simkl"))
        self.assertEqual((first.title, second.title), ("Trakt Title", "Simkl Title"))

    def test_neither_answer_mutates_the_group_or_the_other(self):
        """`resolve` builds a new record; a reader that mutated the stored group
        would hand the next viewer the previous viewer's answer."""
        before = {name: dict(payload)
                  for name, payload in self.group["by_source"].items()}
        calendar_resolve.resolve(self.group, _prefs(default="simkl", fields={"poster": "simkl"}))
        self.assertEqual(self.group["by_source"], before)
        self.assertEqual(calendar_resolve.resolve(self.group).title, "Trakt Title")


class OneSourceIsTheAnswerWhateverIsPreferredTests(unittest.TestCase):
    """A group only one service described has one answer, and no preference can
    turn it into a different one or into none."""

    def test_a_simkl_only_group_resolves_to_simkl_under_a_trakt_preference(self):
        group = _group(_record(Source.SIMKL))
        for precedence in ({"default": "trakt"},
                           {"fields": {"title": "trakt", "poster": "trakt"}},
                           {"default": "trakt", "fields": {"source": "trakt"}}):
            with self.subTest(precedence=precedence):
                record = calendar_resolve.resolve(group, _prefs(**precedence))
                self.assertIsNotNone(record)
                self.assertEqual(str(record.source), "simkl")
                self.assertEqual(record.title, "Simkl Title")
                self.assertEqual(record.poster, "https://simkl.test/p.jpg")

    def test_a_single_source_group_carries_no_provenance_at_all(self):
        """Which is what keeps this free for an instance that has only ever had
        one source: the two maps stay empty and nothing renders a badge."""
        record = calendar_resolve.resolve(_group(_record(Source.TRAKT)))
        self.assertEqual(record.field_sources, {})
        self.assertEqual(record.alternatives, {})


class TheEpisodeCoordinateTests(unittest.TestCase):
    """THE ONE THAT SHIPS AS A VISIBLE DEFECT IF THE MODEL TREATS IT LIKE ANY
    OTHER FIELD.

    Merged groups really do hold a record stating (season 1, episode 1) beside
    one stating (no season, episode 1) — the same airing, said at two
    resolutions. Whichever record supplies the coordinate supplies the S/E chip,
    so a preference that picked the second would render a card with no chip at
    all: not a different label, no label.
    """

    def group(self):
        """The real shape, from two titles observed on a live calendar: a
        complete Trakt coordinate against a Simkl record naming an episode number
        and no season."""
        return _group(
            _record(Source.TRAKT, season=1, episode_number=1, episode_label="S01E01"),
            _record(Source.SIMKL, season=None, episode_number=1, episode_label=None),
        )

    def test_the_label_survives_whichever_source_wins_the_card(self):
        for precedence in ({}, {"default": "simkl"},
                           {"default": "simkl", "fields": {"coordinate": "simkl"}},
                           {"fields": {"coordinate": "simkl", "title": "simkl"}}):
            with self.subTest(precedence=precedence):
                record = calendar_resolve.resolve(self.group(), _prefs(**precedence))
                self.assertEqual(record.season, 1)
                self.assertEqual(record.episode_number, 1)
                self.assertEqual(record.episode_label, "S01E01")

    def test_a_simkl_preference_still_moves_everything_else(self):
        """The coordinate is the exception, not an exemption for the whole
        card."""
        record = calendar_resolve.resolve(self.group(), _prefs(default="simkl"))
        self.assertEqual(str(record.source), "simkl")
        self.assertEqual(record.title, "Simkl Title")
        self.assertEqual(record.episode_label, "S01E01")

    def test_the_season_two_shape_resolves_the_same_way(self):
        """The other observed shape: (2, 1) against (None, 1). Nothing may pair
        Simkl's episode number with a season it never stated."""
        group = _group(
            _record(Source.TRAKT, season=2, episode_number=1, episode_label="S02E01"),
            _record(Source.SIMKL, season=None, episode_number=1, episode_label=None),
        )
        record = calendar_resolve.resolve(group, _prefs(default="simkl"))
        self.assertEqual((record.season, record.episode_number), (2, 1))
        self.assertEqual(record.episode_label, "S02E01")

    def test_a_preference_decides_between_two_equally_complete_coordinates(self):
        """Completeness leads; where both sources said as much as each other,
        the preference is what is left to decide it."""
        group = _forced_group(
            _record(Source.TRAKT, season=1, episode_number=1, episode_label="S01E01"),
            _record(Source.SIMKL, season=3, episode_number=7, episode_label="S03E07"),
        )
        self.assertEqual(calendar_resolve.resolve(group).episode_label, "S01E01")
        self.assertEqual(
            calendar_resolve.resolve(group, _prefs(fields={"coordinate": "simkl"})).episode_label,
            "S03E07")

    def test_a_movie_group_stays_uncoordinated(self):
        """Nothing invents a coordinate for a release that has none."""
        group = _group(
            _record(Source.TRAKT, media="movie", season=None, episode_number=None,
                    episode_label=None),
            _record(Source.SIMKL, media="movie", season=None, episode_number=None,
                    episode_label=None),
        )
        record = calendar_resolve.resolve(group, _prefs(default="simkl"))
        self.assertIsNone(record.season)
        self.assertIsNone(record.episode_label)


class TheFieldsThatAreNotAContestTests(unittest.TestCase):
    """Three answers that are not "pick a winner", each for its own reason."""

    def test_the_ids_are_the_groups_union_whoever_wins_the_card(self):
        group = _group(_record(Source.TRAKT, ids={"tmdb": 900, "trakt": 5}),
                       _record(Source.SIMKL, ids={"tmdb": 900, "simkl": 77, "mal": 6}))
        for precedence in ({}, {"default": "simkl"}):
            with self.subTest(precedence=precedence):
                ids = calendar_resolve.resolve(group, _prefs(**precedence)).ids
                self.assertEqual(ids, {"tmdb": 900, "trakt": 5, "simkl": 77, "mal": 6})

    def test_the_genres_are_a_union_not_a_winner(self):
        """Two services listing different genres have both told the truth, and
        the viewer's genre filter reads the result."""
        group = _group(_record(Source.TRAKT, genres=["drama", "comedy"]),
                       _record(Source.SIMKL, genres=["comedy", "anime"]))
        self.assertEqual(calendar_resolve.resolve(group).genres,
                         ["drama", "comedy", "anime"])
        self.assertEqual(
            calendar_resolve.resolve(group, _prefs(fields={"genres": "simkl"})).genres,
            ["comedy", "anime", "drama"])

    def test_the_air_time_and_its_flag_come_from_one_source_together(self):
        """A timestamp read under the other source's `date_only` renders a film a
        day early for half the world, so the two travel as one answer."""
        group = _group(
            _record(Source.TRAKT, media="movie", air_ts=1000.0, date_only=True),
            _record(Source.SIMKL, media="movie", air_ts=2000.0, date_only=False),
        )
        first = calendar_resolve.resolve(group)
        self.assertEqual((first.air_ts, first.date_only), (1000.0, True))
        second = calendar_resolve.resolve(group, _prefs(fields={"airing": "simkl"}))
        self.assertEqual((second.air_ts, second.date_only), (2000.0, False))


class WhatTheCardIsToldAboutProvenanceTests(unittest.TestCase):
    """`field_sources` and `alternatives`: who supplied each field, and every
    value where they supplied different ones."""

    def setUp(self):
        self.record = calendar_resolve.resolve(
            _group(_record(Source.TRAKT, rating=8.1, network="HBO"),
                   _record(Source.SIMKL, rating=7.9, network="HBO")))

    def test_a_field_both_sources_filled_names_both(self):
        self.assertEqual(self.record.field_sources["rating"], ["trakt", "simkl"])

    def test_only_a_genuine_disagreement_gets_an_alternative(self):
        """Equal values are agreement and have nothing to swap between; a card
        drawing a logo for them would invite a click that changed nothing."""
        self.assertEqual(self.record.alternatives["rating"],
                         {"trakt": 8.1, "simkl": 7.9})
        self.assertNotIn("network", self.record.alternatives)

    def test_a_field_only_one_source_has_names_that_one(self):
        record = calendar_resolve.resolve(
            _group(_record(Source.TRAKT, certification="TV-14"),
                   _record(Source.SIMKL, certification="")))
        self.assertEqual(record.field_sources["certification"], ["trakt"])
        self.assertNotIn("certification", record.alternatives)

    def test_ratings_are_kept_apart_and_never_averaged(self):
        """Two user bases produce two legitimate numbers, and a mean is a number
        nobody reported."""
        self.assertEqual(self.record.rating, 8.1)
        self.assertEqual(sorted(self.record.alternatives["rating"].values()), [7.9, 8.1])

    def test_provenance_is_never_written_into_a_stored_window(self):
        """It is the answer to "what did several sources say when compared",
        which only exists after a read. Storing it would be storing one
        account's comparison in a row served to everybody."""
        self.assertNotIn("field_sources", self.record.to_dict())
        self.assertNotIn("alternatives", self.record.to_dict())


class AStalePreferenceDegradesTests(unittest.TestCase):
    """A row written by a newer version of the app, or naming something that has
    since been retired, must not stop a page rendering."""

    def setUp(self):
        self.group = _group(_record(Source.TRAKT), _record(Source.SIMKL))

    def resolved(self, precedence):
        return calendar_resolve.resolve(
            self.group, source_prefs.SourcePrefs(user_id=1, precedence=precedence))

    def test_an_unknown_source_falls_back_to_the_declared_order(self):
        for precedence in ({"default": "letterboxd"},
                           {"fields": {"title": "letterboxd"}},
                           {"default": "letterboxd", "fields": {"poster": "nobody"}}):
            with self.subTest(precedence=precedence):
                self.assertEqual(self.resolved(precedence).title, "Trakt Title")

    def test_a_field_this_version_does_not_have_is_ignored(self):
        record = self.resolved({"fields": {"tagline": "simkl"}, "default": "simkl"})
        self.assertEqual(record.title, "Simkl Title")

    def test_a_document_that_is_not_shaped_like_one_is_ignored(self):
        for precedence in ({}, {"fields": "simkl"}, {"fields": None},
                           {"default": 7, "fields": {"title": ["simkl"]}},
                           {"unknown": {"title": "simkl"}}):
            with self.subTest(precedence=precedence):
                self.assertEqual(self.resolved(precedence).title, "Trakt Title")

    def test_a_source_the_group_does_not_hold_is_ignored(self):
        group = _group(_record(Source.TRAKT))
        record = calendar_resolve.resolve(group, _prefs(default="simkl"))
        self.assertEqual(record.title, "Trakt Title")


class ChangingAPreferenceInvalidatesNothingTests(unittest.IsolatedAsyncioTestCase):
    """THE DESIGN'S CENTRAL CLAIM, asserted rather than assumed.

    Matching runs at fill and is user-independent; resolution runs at read and is
    per account. The whole reason for that split is that a preference is free —
    changing one must refetch nothing, rewrite nothing and expire nothing, so a
    Sources screen can offer a control that takes effect on the next page load
    without a service being asked anything.

    The proof is by FORBIDDING the fetch, not by observing that none happened:
    every read after the first runs against a fetch that raises, so a refetch
    fails the test instead of merely slowing it down.
    """

    async def asyncSetUp(self):
        new_db_path("calprecedence")
        await db.migrate()
        self.settings = Settings()
        self.window = calendar_cache.window_start(date(2026, 7, 15))

    async def asyncTearDown(self):
        db.close_thread_connection()

    async def fill(self):
        async def fetch(endpoint, settings, start):
            if start != self.window:
                return [], ["trakt", "simkl"]
            return [_record(Source.TRAKT), _record(Source.SIMKL)], ["trakt", "simkl"]
        with patch("app.calendar.cache.fetch_window_records", fetch):
            await self.read(_prefs())

    async def read(self, prefs):
        grouped, _ = await calendar_cache.assemble_range(
            SHOWS, self.settings, tz=ZoneInfo("UTC"),
            start_date=date(2026, 7, 15), end_date=date(2026, 7, 15),
            prefs=prefs, now=1000)
        return [i for g in grouped for i in g["items"]]

    async def stored(self):
        row = await db.fetch_one(
            "SELECT payload, cached_at FROM api_cache WHERE cache_key = ?",
            (calendar_cache.cache_key(SHOWS.key, self.window),))
        return bytes(row["payload"]), row["cached_at"]

    async def test_a_new_preference_needs_no_fetch_and_rewrites_no_row(self):
        await self.fill()
        before = await self.stored()

        def refuse(*args, **kwargs):
            raise AssertionError("a preference change asked a source for data")

        with patch("app.calendar.cache.fetch_window_records", refuse):
            first = await self.read(_prefs())
            second = await self.read(_prefs(default="simkl"))
            third = await self.read(_prefs(fields={"poster": "simkl"}))

        self.assertEqual(first[0].title, "Trakt Title")
        self.assertEqual(second[0].title, "Simkl Title")
        self.assertEqual(third[0].title, "Trakt Title")
        self.assertEqual(third[0].poster, "https://simkl.test/p.jpg")
        # Byte-identical, and cached at the same instant: nothing was rewritten,
        # so nothing expired early either.
        self.assertEqual(await self.stored(), before)

    async def test_the_window_still_holds_every_source_whatever_was_preferred(self):
        """A preference is a reading of the row, never a narrowing of it — the
        next viewer's opposite preference has to have something to find."""
        await self.fill()
        with patch("app.calendar.cache.fetch_window_records",
                   lambda *a, **k: (_ for _ in ()).throw(AssertionError("fetched"))):
            await self.read(_prefs(default="simkl"))
        window, _ = await calendar_cache.read_cached_window(SHOWS.key, self.window)
        self.assertEqual(sorted(s for g in window.groups for s in g["by_source"]),
                         ["simkl", "trakt"])


class WhatEnrichmentGetsToCompeteForTests(unittest.IsolatedAsyncioTestCase):
    """Resolution happens in two halves with the enrichment overlay between
    them, and the ordering is a decision.

    The overlay fills in the fields one source's calendar files do not carry. It
    has to act on THAT SOURCE'S OWN RECORD, before anything picks between the
    sources — otherwise a merged group whose other source supplies the card never
    has its enrichment considered at all, and a value only the enriched source
    knows can never win however the viewer set their preference.
    """

    async def asyncSetUp(self):
        new_db_path("calprecedence-enrich")
        await db.migrate()
        self.settings = Settings()

    async def asyncTearDown(self):
        db.close_thread_connection()

    def group(self):
        """A merged group whose Simkl side arrived with nothing filled in, which
        is what a Simkl calendar record looks like before the drain."""
        return _group(
            _record(Source.TRAKT, overview="Trakt overview.", genres=["drama"]),
            _record(Source.SIMKL, ids={"tmdb": 900, "simkl": 42}, genres=[],
                    overview="", network="", country="", certification="",
                    runtime=None, status="", enriched=False),
        )

    async def enrich(self, **fields):
        from app.calendar import enrich as calendar_enrich
        payload = {"genres": [], "network": "", "country": "", "certification": "",
                   "runtime": None, "status": "", "overview": "", "ids": {}}
        payload.update(fields)
        await calendar_enrich._upsert_success(42, "show", payload, now=999)

    async def resolved(self, prefs):
        from app.calendar import enrich as calendar_enrich
        group = self.group()
        records = calendar_resolve.admitted_records(group, prefs)
        await calendar_enrich.overlay_records(records)
        return calendar_resolve.resolve_records(group, records, prefs)

    async def test_a_preferred_sources_enriched_value_can_win_the_field(self):
        await self.enrich(overview="Simkl overview.")
        record = await self.resolved(_prefs(fields={"overview": "simkl"}))
        self.assertEqual(record.overview, "Simkl overview.")
        # ...and the card is still Trakt's, so exactly the one field moved.
        self.assertEqual(str(record.source), "trakt")

    async def test_an_enriched_value_fills_a_field_the_other_source_left_empty(self):
        await self.enrich(certification="TV-MA")
        record = await self.resolved(_prefs())
        self.assertEqual(record.certification, "TV-14")
        record = await self.resolved(
            _prefs(fields={"certification": "simkl"}))
        self.assertEqual(record.certification, "TV-MA")

    async def test_a_group_is_judgeable_when_any_source_behind_it_has_looked(self):
        """`enriched` is a property of the group, not of the card's source. It
        exists so the filter can tell "nothing to say" from "nobody has looked
        yet" and exempt the second; reading it off the winning source alone would
        exempt a merged card whose genres came, fully filled in, from the other
        service."""
        record = await self.resolved(_prefs(default="simkl"))
        self.assertTrue(record.enriched)
        self.assertEqual(record.genres, ["drama"])

    async def test_a_simkl_only_group_nobody_has_looked_at_is_still_exempt(self):
        group = _group(_record(Source.SIMKL, ids={"tmdb": 900, "simkl": 43},
                               genres=[], enriched=False))
        self.assertFalse(calendar_resolve.resolve(group).enriched)


class ThePerEndpointSelectionTests(unittest.IsolatedAsyncioTestCase):
    """One account, two calendars, two different answers about one service.

    The measured reason: one service's MOVIE calendar is a global release
    calendar contributing well over a thousand entries where the other
    contributes dozens, while the same service's SHOW calendar is coverage worth
    having. A single account-wide selection can only give one answer to that.
    """

    async def asyncSetUp(self):
        new_db_path("calendpointprefs")
        await db.migrate()
        self.settings = Settings()

    async def asyncTearDown(self):
        db.close_thread_connection()

    def group(self):
        return _group(_record(Source.TRAKT), _record(Source.SIMKL))

    def test_an_override_narrows_one_calendar_and_leaves_the_others(self):
        prefs = source_prefs.SourcePrefs(
            user_id=1, calendar_source=source_prefs.AUTO,
            endpoint_sources={"movies": "trakt"})
        self.assertEqual(calendar_resolve.admitted_order(self.group(), prefs, "movies"),
                         ["trakt"])
        self.assertEqual(calendar_resolve.admitted_order(self.group(), prefs, "shows"),
                         ["trakt", "simkl"])
        # And with no endpoint named at all, the account-wide value answers.
        self.assertEqual(calendar_resolve.admitted_order(self.group(), prefs),
                         ["trakt", "simkl"])

    def test_an_override_can_widen_a_narrow_account_wide_choice_too(self):
        """It is a per-calendar statement, not a per-calendar restriction."""
        prefs = source_prefs.SourcePrefs(
            user_id=1, calendar_source="trakt",
            endpoint_sources={"shows": "trakt+simkl"})
        self.assertEqual(calendar_resolve.admitted_order(self.group(), prefs, "shows"),
                         ["trakt", "simkl"])
        self.assertEqual(calendar_resolve.admitted_order(self.group(), prefs, "movies"),
                         ["trakt"])

    async def test_the_override_reaches_the_read_path(self):
        async def fetch(endpoint, settings, start):
            return [_record(Source.TRAKT), _record(Source.SIMKL)], ["trakt", "simkl"]

        prefs = source_prefs.SourcePrefs(
            user_id=1, endpoint_sources={SHOWS.key: "simkl"})
        with patch("app.calendar.cache.fetch_window_records", fetch):
            grouped, _ = await calendar_cache.assemble_range(
                SHOWS, self.settings, tz=ZoneInfo("UTC"),
                start_date=date(2026, 7, 15), end_date=date(2026, 7, 15),
                prefs=prefs, now=1000)
        titles = [i.title for g in grouped for i in g["items"]]
        self.assertEqual(titles, ["Simkl Title"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
