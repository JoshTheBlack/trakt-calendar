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
from urllib.parse import quote
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app import db
from app.calendar import cache as calendar_cache, resolve as calendar_resolve
from app.calendar import state as calendar_state
from app.providers import base as providers_base
from app.config import Settings
from app.endpoints import get_endpoint
from app.providers.base import Record, Source
from app.sources import prefs as source_prefs
from tests.support import migrated_db

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


def _prefs(*order) -> source_prefs.SourcePrefs:
    """An account preferring `order`, most preferred first. No arguments is an
    account that has stated nothing, which is what almost every one is."""
    return source_prefs.SourcePrefs(user_id=1, metadata_order=list(order))


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


class ThePreferredSourceLeadsEveryFieldTests(unittest.TestCase):
    """ONE ORDER, EVERY FIELD, where this was once answerable per field.

    The per-field half is gone: an account could name one service for the
    overview and another for the poster, which is eleven questions where the one
    anybody asks is "prefer this service", and it was the reason the preference
    needed a screen of its own. What survives is the question people do ask.
    """

    def setUp(self):
        self.group = _group(_record(Source.TRAKT), _record(Source.SIMKL))
        self.baseline = calendar_resolve.resolve(self.group)

    def test_the_preferred_source_wins_every_field_it_answers_for(self):
        record = calendar_resolve.resolve(self.group, _prefs("simkl"))
        self.assertEqual(record.title, "Simkl Title")
        self.assertEqual(record.overview, "Simkl overview.")
        self.assertEqual(record.poster, "https://simkl.test/p.jpg")
        # ...including the card's own identity, which is a field of its own.
        self.assertEqual(str(record.source), "simkl")

    def test_the_declared_order_stands_where_nothing_is_stated(self):
        record = calendar_resolve.resolve(self.group, _prefs())
        self.assertEqual(record.title, self.baseline.title)
        self.assertEqual(str(record.source), "trakt")

    def test_naming_the_second_of_two_says_what_leads_and_what_follows(self):
        """With a third service registered, "Simkl first" would say nothing
        about the other two — which is why the whole order is stored rather than
        one promoted name."""
        record = calendar_resolve.resolve(self.group, _prefs("simkl", "trakt"))
        self.assertEqual(str(record.source), "simkl")

    def test_two_accounts_read_one_group_and_get_different_answers(self):
        """The same stored group, two opposite preferences, no copying and no
        second row anywhere."""
        first = calendar_resolve.resolve(self.group, _prefs("trakt"))
        second = calendar_resolve.resolve(self.group, _prefs("simkl"))
        self.assertEqual((first.title, second.title), ("Trakt Title", "Simkl Title"))

    def test_neither_answer_mutates_the_group_or_the_other(self):
        """`resolve` builds a new record; a reader that mutated the stored group
        would hand the next viewer the previous viewer's answer."""
        before = {name: dict(payload)
                  for name, payload in self.group["by_source"].items()}
        calendar_resolve.resolve(self.group, _prefs("simkl"))
        self.assertEqual(self.group["by_source"], before)
        self.assertEqual(calendar_resolve.resolve(self.group).title, "Trakt Title")


class OneSourceIsTheAnswerWhateverIsPreferredTests(unittest.TestCase):
    """A group only one service described has one answer, and no preference can
    turn it into a different one or into none."""

    def test_a_simkl_only_group_resolves_to_simkl_under_a_trakt_preference(self):
        group = _group(_record(Source.SIMKL))
        for order in (("trakt",), ("trakt", "simkl")):
            with self.subTest(order=order):
                record = calendar_resolve.resolve(group, _prefs(*order))
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
        for order in ((), ("simkl",), ("simkl", "trakt"), ("trakt", "simkl")):
            with self.subTest(order=order):
                record = calendar_resolve.resolve(self.group(), _prefs(*order))
                self.assertEqual(record.season, 1)
                self.assertEqual(record.episode_number, 1)
                self.assertEqual(record.episode_label, "S01E01")

    def test_a_simkl_preference_still_moves_everything_else(self):
        """The coordinate is the exception, not an exemption for the whole
        card."""
        record = calendar_resolve.resolve(self.group(), _prefs("simkl"))
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
        record = calendar_resolve.resolve(group, _prefs("simkl"))
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
            calendar_resolve.resolve(group, _prefs("simkl")).episode_label,
            "S03E07")

    def test_a_movie_group_stays_uncoordinated(self):
        """Nothing invents a coordinate for a release that has none."""
        group = _group(
            _record(Source.TRAKT, media="movie", season=None, episode_number=None,
                    episode_label=None),
            _record(Source.SIMKL, media="movie", season=None, episode_number=None,
                    episode_label=None),
        )
        record = calendar_resolve.resolve(group, _prefs("simkl"))
        self.assertIsNone(record.season)
        self.assertIsNone(record.episode_label)


class TheFieldsThatAreNotAContestTests(unittest.TestCase):
    """Three answers that are not "pick a winner", each for its own reason."""

    def test_the_ids_are_the_groups_union_whoever_wins_the_card(self):
        group = _group(_record(Source.TRAKT, ids={"tmdb": 900, "trakt": 5}),
                       _record(Source.SIMKL, ids={"tmdb": 900, "simkl": 77, "mal": 6}))
        for order in ((), ("simkl",)):
            with self.subTest(order=order):
                ids = calendar_resolve.resolve(group, _prefs(*order)).ids
                self.assertEqual(ids, {"tmdb": 900, "trakt": 5, "simkl": 77, "mal": 6})

    def test_the_genres_are_a_union_not_a_winner(self):
        """Two services listing different genres have both told the truth, and
        the viewer's genre filter reads the result."""
        group = _group(_record(Source.TRAKT, genres=["drama", "comedy"]),
                       _record(Source.SIMKL, genres=["comedy", "anime"]))
        self.assertEqual(calendar_resolve.resolve(group).genres,
                         ["drama", "comedy", "anime"])
        self.assertEqual(
            calendar_resolve.resolve(group, _prefs("simkl")).genres,
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
        second = calendar_resolve.resolve(group, _prefs("simkl"))
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

    def resolved(self, order):
        return calendar_resolve.resolve(
            self.group, source_prefs.SourcePrefs(user_id=1, metadata_order=order))

    def test_an_unknown_source_falls_back_to_the_declared_order(self):
        for order in (["letterboxd"], ["letterboxd", "nobody"]):
            with self.subTest(order=order):
                self.assertEqual(self.resolved(order).title, "Trakt Title")

    def test_a_known_source_behind_an_unknown_one_still_leads(self):
        """A retired service in front of a real one must not take the real one's
        turn with it."""
        self.assertEqual(self.resolved(["letterboxd", "simkl"]).title, "Simkl Title")

    def test_a_document_that_is_not_shaped_like_one_is_ignored(self):
        # A bare string is NOT here: it reads as a one-element order, which is
        # how a stored preference was spelled before an order was possible.
        for order in ([], None, 7, [7, None], {"default": "simkl"}):
            with self.subTest(order=order):
                self.assertEqual(self.resolved(order).title, "Trakt Title")

    def test_a_source_the_group_does_not_hold_is_ignored(self):
        group = _group(_record(Source.TRAKT))
        record = calendar_resolve.resolve(group, _prefs("simkl"))
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
        migrated_db("calprecedence")
        self.settings = Settings()
        self.window = calendar_cache.window_start(date(2026, 7, 15))

    async def asyncTearDown(self):
        db.close_thread_connection()

    async def fill(self):
        async def fetch(endpoint, settings, start, *, covered=()):
            if start != self.window:
                return [], ["trakt", "simkl"], []
            return [_record(Source.TRAKT), _record(Source.SIMKL)], ["trakt", "simkl"], []
        with patch("app.calendar.cache.fetch_window_records", fetch):
            await self.read(_prefs())

    async def read(self, prefs):
        grouped, _ = await calendar_cache.assemble_range(
            SHOWS, self.settings, tz=ZoneInfo("UTC"),
            start_date=date(2026, 7, 15), end_date=date(2026, 7, 15),
            prefs=prefs, now=1000)
        return [i for g in grouped for i in g["items"]]

    async def stored(self):
        """Everything the calendar holds for this span, as a comparable value.

        The claim is unchanged — a preference change must rewrite nothing — but
        there is no longer a blob whose bytes can be compared. What stands in for
        it is every row the span owns, INCLUDING each row's `stored_at`, so a
        rewrite that happened to produce identical values is still caught.
        """
        airings = await db.fetch_all(
            "SELECT source, source_id, air_ts, stored_at FROM calendar_airings "
            "WHERE endpoint = ? ORDER BY source, source_id, air_ts", (SHOWS.key,))
        coverage = await db.fetch_all(
            "SELECT source, asked, answered, stored_at FROM calendar_coverage "
            "WHERE endpoint = ? AND span_start = ? ORDER BY source",
            (SHOWS.key, self.window.isoformat()))
        titles = await db.fetch_all(
            "SELECT source, source_id, fetched_at FROM calendar_titles "
            "ORDER BY source, source_id")
        return ([tuple(r) for r in airings], [tuple(r) for r in coverage],
                [tuple(r) for r in titles])

    async def test_a_new_preference_needs_no_fetch_and_rewrites_no_row(self):
        await self.fill()
        before = await self.stored()

        def refuse(*args, **kwargs):
            raise AssertionError("a preference change asked a source for data")

        with patch("app.calendar.cache.fetch_window_records", refuse):
            first = await self.read(_prefs())
            second = await self.read(_prefs("simkl"))
            third = await self.read(_prefs("trakt", "simkl"))

        self.assertEqual(first[0].title, "Trakt Title")
        self.assertEqual(second[0].title, "Simkl Title")
        # ...and back again, on a third reading of the same rows.
        self.assertEqual(third[0].title, "Trakt Title")
        # A RENDERED poster is display-form, so what is asserted is that the
        # preference moved it to SIMKL'S picture — the origin the proxy was
        # handed — rather than the exact string, which the proxy's measured
        # parameters will move again.
        self.assertIn(quote("https://simkl.test/p.jpg", safe=""), second[0].poster)
        # Byte-identical, and cached at the same instant: nothing was rewritten,
        # so nothing expired early either.
        self.assertEqual(await self.stored(), before)

    async def test_the_window_still_holds_every_source_whatever_was_preferred(self):
        """A preference is a reading of the row, never a narrowing of it — the
        next viewer's opposite preference has to have something to find."""
        await self.fill()
        with patch("app.calendar.cache.fetch_window_records",
                   lambda *a, **k: (_ for _ in ()).throw(AssertionError("fetched"))):
            await self.read(_prefs("simkl"))
        window, _ = await calendar_cache.read_cached_window(SHOWS.key, self.window)
        self.assertEqual(sorted(s for g in window.groups for s in g["by_source"]),
                         ["simkl", "trakt"])


class WhatEnrichmentGetsToCompeteForTests(unittest.IsolatedAsyncioTestCase):
    """Resolution happens in two halves with enrichment applied between them, and
    the ordering is a decision.

    Enrichment fills in the fields one source's calendar files do not carry. It
    has to act on THAT SOURCE'S OWN RECORD, before anything picks between the
    sources — otherwise a merged group whose other source supplies the card never
    has its enrichment considered at all, and a value only the enriched source
    knows can never win however the viewer set their preference.

    IT IS APPLIED AT STORAGE NOW RATHER THAN AT READ, and the ordering claim is
    untouched by that: what these pin is that a source's own record carries its
    own answer by the time resolution sees it, whichever side of the write that
    happened on.
    """

    async def asyncSetUp(self):
        migrated_db("calprecedence-enrich")
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
        await calendar_enrich.apply_stored_enrichment(records)
        return calendar_resolve.resolve_records(group, records, prefs)

    async def test_a_preferred_sources_enriched_value_can_win_the_field(self):
        await self.enrich(overview="Simkl overview.")
        record = await self.resolved(_prefs("simkl"))
        self.assertEqual(record.overview, "Simkl overview.")
        # The card is Simkl's too — ONE ORDER MOVES EVERY FIELD, and what this
        # test is about is that an ENRICHED value competes at all rather than
        # being invisible to the preference.
        self.assertEqual(str(record.source), "simkl")

    async def test_an_enriched_value_fills_a_field_the_other_source_left_empty(self):
        await self.enrich(certification="TV-MA")
        record = await self.resolved(_prefs())
        self.assertEqual(record.certification, "TV-14")
        record = await self.resolved(
            _prefs("simkl"))
        self.assertEqual(record.certification, "TV-MA")

    async def test_a_group_is_judgeable_when_any_source_behind_it_has_looked(self):
        """`enriched` is a property of the group, not of the card's source. It
        exists so the filter can tell "nothing to say" from "nobody has looked
        yet" and exempt the second; reading it off the winning source alone would
        exempt a merged card whose genres came, fully filled in, from the other
        service."""
        record = await self.resolved(_prefs("simkl"))
        self.assertTrue(record.enriched)
        self.assertEqual(record.genres, ["drama"])

    async def test_a_simkl_only_group_nobody_has_looked_at_is_still_exempt(self):
        group = _group(_record(Source.SIMKL, ids={"tmdb": 900, "simkl": 43},
                               genres=[], enriched=False))
        self.assertFalse(calendar_resolve.resolve(group).enriched)




class AMarkSticksToTheTitleNotToWhoDescribesItTests(unittest.TestCase):
    """A per-viewer MARK may not move when a per-viewer PREFERENCE does.

    THE LIVE CASE. One account had `the-game` marked not-watching and
    hide-not-watching on. Trakt and Simkl both list that show — same tmdb id, one
    merged group — and they spell its id differently: `the-game-2025` and
    `the-game`. The card carried whichever id had WON, so reordering the metadata
    preference changed the card's identity, the mark stopped matching, and a show
    the viewer had hidden came back. The same reorder hid a different show for the
    mirror-image reason.

    So a card's identity is its GROUP's — the same cross-source waterfall the
    grouping itself uses — and nothing a preference touches can move it.
    """

    def setUp(self):
        self.group = _group(_record(Source.TRAKT), _record(Source.SIMKL))

    def _rendered(self, *order):
        record = calendar_resolve.resolve(self.group, _prefs(*order))
        return providers_base.render(record, ZoneInfo("UTC"))

    def test_the_mark_key_does_not_move_when_the_preference_does(self):
        trakt_first = self._rendered("trakt", "simkl")
        simkl_first = self._rendered("simkl", "trakt")
        # The winning source DID move — otherwise this test proves nothing.
        self.assertNotEqual(str(trakt_first.source), str(simkl_first.source))
        self.assertEqual(trakt_first.mark_key, simkl_first.mark_key)

    def test_the_key_is_the_shared_identity_rather_than_either_id(self):
        record = self._rendered()
        self.assertEqual(record.mark_key, "show:tmdb:900")

    def test_a_title_no_shared_id_space_names_falls_back_to_its_own_source(self):
        """Safe precisely because such a title never merges: there is only one
        description of it, so there is nothing for a preference to move."""
        lonely = _group(_record(Source.SIMKL, id="unkeyable", ids={"slug": "u"}))
        record = providers_base.render(
            calendar_resolve.resolve(lonely, _prefs()), ZoneInfo("UTC"))
        self.assertEqual(record.mark_key, "simkl:unkeyable")

    def test_a_mark_made_under_either_spelling_still_matches(self):
        """The tolerance that meant no stored mark had to be thrown away: one
        real account held 909 of them, 109 naming titles nothing currently
        stores, so they could not have been rewritten in advance."""
        trakt_first = self._rendered("trakt", "simkl")
        simkl_first = self._rendered("simkl", "trakt")
        legacy = {str(simkl_first.id)}
        self.assertTrue(calendar_state.marked(legacy, simkl_first))
        self.assertTrue(calendar_state.marked({trakt_first.mark_key}, trakt_first))
        self.assertFalse(calendar_state.marked({"something-else"}, trakt_first))

    def test_a_mark_made_under_a_slug_survives_the_record_being_re_keyed(self):
        """THE OTHER WAY A CARD'S IDENTITY CAN MOVE, and it moved for real.

        Simkl's slugs are not unique — two live shows share `brothers` — so its
        records stopped being keyed by slug and started being keyed by simkl id.
        Every mark made against a Simkl-described card had been stored under the
        slug, and on one real September 118 of them stopped matching at once:
        LOVESICK, Chad Powers, The Gentlemen, and a hundred more, all silently
        un-hidden.

        A stored mark is a fact about a TITLE. Which spelling it happens to be
        written in is not, so every spelling the card has ever been identified by
        is accepted.
        """
        record = calendar_resolve.resolve(
            _group(_record(Source.SIMKL, id="3101847",
                           ids={"tmdb": 900, "simkl": 3101847, "slug": "lovesick"})),
            _prefs())
        item = providers_base.render(record, ZoneInfo("UTC"))
        self.assertEqual(str(item.id), "3101847")
        self.assertTrue(calendar_state.marked({"lovesick"}, item))

    def test_a_bare_service_number_is_not_a_spelling_of_the_title(self):
        """A card was never identified by a bare tmdb or tvdb number, and
        admitting them would hide a title nobody marked: one real account has a
        mark spelled `1670` — that show's slug — and tmdb 1670 is a different
        programme. A false match here leaves no trace on the page."""
        record = calendar_resolve.resolve(
            _group(_record(Source.TRAKT, id="some-show",
                           ids={"tmdb": 1670, "trakt": 55, "slug": "some-show"})),
            _prefs())
        item = providers_base.render(record, ZoneInfo("UTC"))
        self.assertFalse(calendar_state.marked({"1670"}, item))
        self.assertTrue(calendar_state.marked({"some-show"}, item))
