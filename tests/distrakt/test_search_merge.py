"""The merge behind the tracker's manual add flow (app/distrakt/search.py).

Every case here is built out of hand-made SearchHits — no provider, no
settings that mean anything, no network — because the whole reason the merge
is a pure function is that the dedupe rules can be trusted without any of
those. See app/distrakt/search.py's own docstring for the shape being tested.
"""
from __future__ import annotations

import asyncio
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.distrakt import search
from app.providers.base import ItemKey, Media, SearchHit, Source

SETTINGS = SimpleNamespace()


def hit(source, source_id, *, tmdb=None, mal=None, season=None, title="Title",
        year=2020, network="", runtime=None, overview="") -> SearchHit:
    ids = {}
    if tmdb is not None:
        ids["tmdb"] = tmdb
    if mal is not None:
        ids["mal"] = mal
    return SearchHit(source=source, source_id=source_id, media=Media.SHOW, ids=ids,
                     title=title, year=year, season=season, network=network,
                     runtime=runtime, overview=overview)


class DedupeTests(unittest.TestCase):
    def test_two_sources_naming_the_same_title_merge_into_one_row(self):
        result = search.merge_search_hits([
            (Source.TRAKT, [hit(Source.TRAKT, "1", tmdb="1429", title="Attack on Titan")]),
            (Source.SIMKL, [hit(Source.SIMKL, "44", tmdb="1429", title="Attack on Titan")]),
        ])
        self.assertEqual(len(result.hits), 1)
        self.assertEqual(result.hits[0].key, ItemKey("show", "tmdb", "1429"))
        self.assertEqual(set(result.hits[0].source_ids), {Source.TRAKT, Source.SIMKL})

    def test_the_dedupe_unit_is_key_and_season_not_key_alone(self):
        """The Attack on Titan case, measured: S2 and S3 both resolve to
        show:tmdb:1429. Deduping on the key alone would collapse three
        seasons into one row and silently drop two of them."""
        result = search.merge_search_hits([
            (Source.SIMKL, [
                hit(Source.SIMKL, "1", tmdb="1429", season=None, title="Attack on Titan"),
                hit(Source.SIMKL, "2", tmdb="1429", season=2, title="Attack on Titan Season 2"),
                hit(Source.SIMKL, "3", tmdb="1429", season=3, title="Attack on Titan Season 3"),
            ]),
        ])
        self.assertEqual(len(result.hits), 3)
        self.assertEqual({(h.key, h.season) for h in result.hits}, {
            (ItemKey("show", "tmdb", "1429"), None),
            (ItemKey("show", "tmdb", "1429"), 2),
            (ItemKey("show", "tmdb", "1429"), 3),
        })

    def test_a_hit_with_no_resolvable_key_stands_alone(self):
        result = search.merge_search_hits([
            (Source.SIMKL, [hit(Source.SIMKL, "694485", title="Shingeki no Kyojin Season 3")]),
        ])
        self.assertEqual(len(result.hits), 1)
        self.assertIsNone(result.hits[0].key)
        self.assertEqual(dict(result.hits[0].source_ids), {Source.SIMKL: "694485"})

    def test_two_unrelated_unkeyable_hits_do_not_merge_with_each_other(self):
        """Neither hit can be told apart from the other by anything this
        function is willing to call an identity, so both survive rather than
        colliding on some shared "no key" bucket."""
        result = search.merge_search_hits([
            (Source.SIMKL, [
                hit(Source.SIMKL, "1", title="Show A"),
                hit(Source.SIMKL, "2", title="Show B"),
            ]),
        ])
        self.assertEqual(len(result.hits), 2)
        self.assertEqual({h.title for h in result.hits}, {"Show A", "Show B"})

    def test_a_single_source_hit_carries_only_that_sources_mark(self):
        result = search.merge_search_hits([
            (Source.TRAKT, [hit(Source.TRAKT, "1", tmdb="1429")]),
            (Source.SIMKL, [hit(Source.SIMKL, "9", tmdb="999999")]),
        ])
        self.assertEqual(len(result.hits), 2)
        for merged in result.hits:
            self.assertEqual(len(merged.source_ids), 1)


class LeaderIdTests(unittest.TestCase):
    """Which id a merged row calls back with — the address a pick sends to
    /api/distrakt/seasons, and the one that has to describe the title the row
    is actually showing."""

    def test_one_source_returning_a_title_twice_gives_two_rows(self):
        """MEASURED 2026-08-18 on a Simkl-only instance: "frieren" returns
        three titles that all resolve to show:tmdb:209867 and all carry no
        season, so under one slot per (key, season) they became ONE row and
        two were lost — Beastars season 2 could not be surfaced by any query
        at all. A source returning three entries is saying it holds three
        things, and this function's dedupe is about two SERVICES describing one
        title, not about a service's own catalogue.
        """
        result = search.merge_search_hits([
            (Source.SIMKL, [
                hit(Source.SIMKL, "1990194", tmdb="209867", title="Sousou no Frieren",
                    year=2023),
                hit(Source.SIMKL, "2595284", tmdb="209867", title="Sousou no Frieren",
                    year=2026),
                hit(Source.SIMKL, "3063278", tmdb="209867", title="Sousou no Frieren",
                    year=2027),
            ]),
        ])
        self.assertEqual([r.source_ids[Source.SIMKL] for r in result.hits],
                         ["1990194", "2595284", "3063278"])
        # Each still keeps its own identity — one key, three reachable titles.
        self.assertEqual({r.key for r in result.hits},
                         {ItemKey("show", "tmdb", "209867")})
        self.assertEqual([r.year for r in result.hits], [2023, 2026, 2027])

    def test_a_row_addresses_the_title_it_draws(self):
        """The failure the two rules above exist to prevent, stated as one
        property: whatever a row shows, the id a pick sends back names it."""
        result = search.merge_search_hits([
            (Source.SIMKL, [
                hit(Source.SIMKL, "1034467", tmdb="90937", title="Beastars", year=2019),
                hit(Source.SIMKL, "1231401", tmdb="90937", title="Beastars", year=2021),
                hit(Source.SIMKL, "2831384", tmdb="90937", title="Beastars Final Season",
                    year=2026),
            ]),
        ])
        drawn = {(r.title, r.year): r.source_ids[Source.SIMKL] for r in result.hits}
        self.assertEqual(drawn, {("Beastars", 2019): "1034467",
                                 ("Beastars", 2021): "1231401",
                                 ("Beastars Final Season", 2026): "2831384"})

    def test_two_services_naming_one_title_still_merge(self):
        """The dedupe that IS wanted, unaffected: both marks on one row, and
        registry order still decides who answers for it."""
        result = search.merge_search_hits([
            (Source.TRAKT, [hit(Source.TRAKT, "t1", tmdb="1429")]),
            (Source.SIMKL, [hit(Source.SIMKL, "s1", tmdb="1429")]),
        ])
        row, = result.hits
        self.assertEqual(row.source_ids, {Source.TRAKT: "t1", Source.SIMKL: "s1"})
        self.assertEqual(next(iter(row.source_ids)), Source.TRAKT)

    def test_one_source_naming_the_same_season_twice_is_one_row(self):
        """The other half of the rule. Simkl lists "Beastars Final Season"
        twice — two catalogue titles, mal 49469 and 61114 — and both name
        season 3 of show:tmdb:90937. That is one (key, season), which is the
        single unit the tracker can file a record under, so two rows here would
        offer two ways to add the identical record."""
        result = search.merge_search_hits([
            (Source.SIMKL, [
                hit(Source.SIMKL, "1687953", tmdb="90937", season=3,
                    title="Beastars Final Season", year=2021),
                hit(Source.SIMKL, "2831384", tmdb="90937", season=3,
                    title="Beastars Final Season", year=2026),
            ]),
        ])
        row, = result.hits
        self.assertEqual(row.season, 3)
        self.assertEqual(row.source_ids[Source.SIMKL], "1687953")

    def test_named_seasons_of_one_series_stay_apart(self):
        """And naming them is what keeps the genuinely different ones
        distinct: one key, three seasons, three rows."""
        result = search.merge_search_hits([
            (Source.SIMKL, [
                hit(Source.SIMKL, "1990194", tmdb="209867", season=1, title="Frieren"),
                hit(Source.SIMKL, "2595284", tmdb="209867", season=2, title="Frieren"),
                hit(Source.SIMKL, "3063278", tmdb="209867", season=3, title="Frieren"),
            ]),
        ])
        self.assertEqual([(r.season, r.source_ids[Source.SIMKL]) for r in result.hits],
                         [(1, "1990194"), (2, "2595284"), (3, "3063278")])

    def test_a_named_hit_does_not_join_an_unnamed_row(self):
        """(key, 3) and (key, None) are different dedupe units, so a hit whose
        season is known never merges into one whose season is not — the second
        might be any season, including that one."""
        result = search.merge_search_hits([
            (Source.SIMKL, [
                hit(Source.SIMKL, "a", tmdb="90937", season=None, title="Beastars"),
                hit(Source.SIMKL, "b", tmdb="90937", season=3, title="Beastars Final"),
            ]),
        ])
        self.assertEqual([(r.season, r.source_ids[Source.SIMKL]) for r in result.hits],
                         [(None, "a"), (3, "b")])

    def test_a_whole_show_row_absorbs_another_sources_season_rows(self):
        """Trakt models a series as ONE show with a picker; Simkl files each
        season as its own title. Left alone the viewer sees "Beastars" beside
        "Beastars Season 2" as if they were alternatives, when the first
        reaches all of them — and the linked row loses both its marks."""
        result = search.merge_search_hits([
            (Source.TRAKT, [hit(Source.TRAKT, "t1", tmdb="90937", title="Beastars",
                                network="Netflix")]),
            (Source.SIMKL, [hit(Source.SIMKL, "1034467", tmdb="90937", season=1),
                            hit(Source.SIMKL, "1231401", tmdb="90937", season=2),
                            hit(Source.SIMKL, "2831384", tmdb="90937", season=3)]),
        ])
        row, = result.hits
        self.assertIsNone(row.season)
        self.assertEqual(row.network, "Netflix")
        # Both marks are drawn again, and Trakt still answers for the row.
        self.assertEqual(list(row.source_ids), [Source.TRAKT, Source.SIMKL])
        self.assertEqual(row.source_ids[Source.SIMKL], "1034467")

    def test_a_season_row_is_not_absorbed_by_its_own_sources_unnamed_row(self):
        """A source's own season-less row can mean "this is the show" OR "a
        per-title lookup failed and nobody knows what this is". Absorbing on
        that would let the second case swallow real results — the collapse the
        dedupe rules exist to prevent."""
        result = search.merge_search_hits([
            (Source.SIMKL, [hit(Source.SIMKL, "a", tmdb="90937", season=None),
                            hit(Source.SIMKL, "b", tmdb="90937", season=2)]),
        ])
        self.assertEqual([(r.season, r.source_ids[Source.SIMKL]) for r in result.hits],
                         [(None, "a"), (2, "b")])

    def test_seasons_of_a_series_no_other_source_returned_all_stand(self):
        """The single-catalogue instance: nothing supersedes them, so every
        season stays reachable."""
        result = search.merge_search_hits([
            (Source.SIMKL, [hit(Source.SIMKL, "1034467", tmdb="90937", season=1),
                            hit(Source.SIMKL, "1231401", tmdb="90937", season=2)]),
        ])
        self.assertEqual([r.season for r in result.hits], [1, 2])

    def test_a_later_source_joins_the_earliest_row_still_open_to_it(self):
        """One Trakt hit against a source that returned the same key three
        times: the Trakt hit joins the FIRST of them rather than opening a
        fourth row or being spread across all three."""
        result = search.merge_search_hits([
            (Source.TRAKT, [hit(Source.TRAKT, "t1", tmdb="90937", title="Beastars")]),
            (Source.SIMKL, [hit(Source.SIMKL, "s1", tmdb="90937", title="Beastars"),
                            hit(Source.SIMKL, "s2", tmdb="90937", title="Beastars"),
                            hit(Source.SIMKL, "s3", tmdb="90937", title="Beastars")]),
        ])
        self.assertEqual([dict(r.source_ids) for r in result.hits], [
            {Source.TRAKT: "t1", Source.SIMKL: "s1"},
            {Source.SIMKL: "s2"},
            {Source.SIMKL: "s3"},
        ])


class FieldMergeTests(unittest.TestCase):
    def test_registry_order_leads_and_a_blank_field_is_filled_from_the_follower(self):
        simkl_first = hit(Source.SIMKL, "9", tmdb="1429", title="Severance",
                          network="", runtime=None, overview="")
        trakt_second = hit(Source.TRAKT, "1", tmdb="1429", title="Severance (Trakt)",
                           network="Apple TV+", runtime=53, overview="A workplace.")
        result = search.merge_search_hits([
            (Source.SIMKL, [simkl_first]),
            (Source.TRAKT, [trakt_second]),
        ])
        merged = result.hits[0]
        # the leader's title wins even though the follower's differs slightly —
        # only the BLANK fields are filled from elsewhere.
        self.assertEqual(merged.title, "Severance")
        self.assertEqual(merged.network, "Apple TV+")
        self.assertEqual(merged.runtime, 53)
        self.assertEqual(merged.overview, "A workplace.")

    def test_a_field_the_leader_already_has_is_not_overwritten(self):
        leader = hit(Source.TRAKT, "1", tmdb="1429", network="Apple TV+", runtime=53)
        follower = hit(Source.SIMKL, "9", tmdb="1429", network="Somewhere Else", runtime=99)
        result = search.merge_search_hits([
            (Source.TRAKT, [leader]),
            (Source.SIMKL, [follower]),
        ])
        merged = result.hits[0]
        self.assertEqual(merged.network, "Apple TV+")
        self.assertEqual(merged.runtime, 53)

    def test_ids_are_unioned_unconditionally(self):
        """The quiet win of merging at all: a title Trakt knows by tmdb and
        Simkl knows by mal comes out carrying both."""
        trakt_hit = hit(Source.TRAKT, "1", tmdb="1429")
        simkl_hit = hit(Source.SIMKL, "9", tmdb="1429", mal="5678")
        result = search.merge_search_hits([
            (Source.TRAKT, [trakt_hit]),
            (Source.SIMKL, [simkl_hit]),
        ])
        self.assertEqual(result.hits[0].ids, {"tmdb": "1429", "mal": "5678"})

    def test_a_merge_of_materially_different_titles_is_logged(self):
        """Two hits should never actually land on one key with unrelated
        titles — this is the operator-facing tripwire if the dedupe unit
        ever behaves unexpectedly, not a refusal."""
        with self.assertLogs("app.distrakt.search", level="WARNING") as logs:
            search.merge_search_hits([
                (Source.TRAKT, [hit(Source.TRAKT, "1", tmdb="1429", title="Show One")]),
                (Source.SIMKL, [hit(Source.SIMKL, "9", tmdb="1429", title="Totally Different")]),
            ])
        self.assertTrue(any("Show One" in line and "Totally Different" in line
                            for line in logs.output))


class FailedPassthroughTests(unittest.TestCase):
    def test_failed_is_threaded_through_unchanged(self):
        result = search.merge_search_hits([], failed=frozenset({Source.SIMKL}))
        self.assertEqual(result.hits, [])
        self.assertEqual(result.failed, frozenset({Source.SIMKL}))

    def test_no_hits_and_no_failures_is_a_real_empty_search(self):
        result = search.merge_search_hits([(Source.TRAKT, [])])
        self.assertEqual(result.hits, [])
        self.assertEqual(result.failed, frozenset())


def _port(*hits, error=None):
    async def _search_titles(_settings, _media, _query):
        if error is not None:
            raise error
        return list(hits)
    return SimpleNamespace(search_titles=AsyncMock(side_effect=_search_titles))


class SearchCatalogueTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_sources_asked_makes_no_calls_and_answers_empty(self):
        result = await search.search_catalogue([], SETTINGS, Media.SHOW, "x")
        self.assertEqual(result, search.SearchMergeResult(hits=[], failed=frozenset()))

    async def test_every_source_answers_and_the_results_are_merged(self):
        trakt = _port(hit(Source.TRAKT, "1", tmdb="1429", title="Severance"))
        simkl = _port(hit(Source.SIMKL, "9", tmdb="1429", title="Severance"))
        result = await search.search_catalogue(
            [(Source.TRAKT, trakt), (Source.SIMKL, simkl)], SETTINGS, Media.SHOW, "severance")
        self.assertEqual(len(result.hits), 1)
        self.assertEqual(set(result.hits[0].source_ids), {Source.TRAKT, Source.SIMKL})
        self.assertEqual(result.failed, frozenset())

    async def test_one_source_failing_does_not_lose_the_others_hits(self):
        trakt = _port(hit(Source.TRAKT, "1", tmdb="1429", title="Severance"))
        simkl = _port(error=RuntimeError("down"))
        result = await search.search_catalogue(
            [(Source.TRAKT, trakt), (Source.SIMKL, simkl)], SETTINGS, Media.SHOW, "severance")
        self.assertEqual(len(result.hits), 1)
        self.assertEqual(set(result.hits[0].source_ids), {Source.TRAKT})
        self.assertEqual(result.failed, frozenset({Source.SIMKL}))

    async def test_every_source_failing_is_distinguishable_from_no_matches(self):
        trakt = _port(error=RuntimeError("down"))
        simkl = _port(error=RuntimeError("also down"))
        result = await search.search_catalogue(
            [(Source.TRAKT, trakt), (Source.SIMKL, simkl)], SETTINGS, Media.SHOW, "x")
        self.assertEqual(result.hits, [])
        self.assertEqual(result.failed, frozenset({Source.TRAKT, Source.SIMKL}))

    async def test_sources_are_asked_concurrently_not_one_after_another(self):
        """Guards `asyncio.gather` staying gather — a caller that switched
        this to sequential awaits would still pass every other test here."""
        order: list[str] = []

        async def _slow(_settings, _media, _query):
            order.append("trakt-start")
            await asyncio.sleep(0.02)
            order.append("trakt-end")
            return []

        async def _fast(_settings, _media, _query):
            order.append("simkl-start")
            order.append("simkl-end")
            return []

        trakt = SimpleNamespace(search_titles=_slow)
        simkl = SimpleNamespace(search_titles=_fast)
        await search.search_catalogue(
            [(Source.TRAKT, trakt), (Source.SIMKL, simkl)], SETTINGS, Media.SHOW, "x")
        self.assertEqual(order, ["trakt-start", "simkl-start", "simkl-end", "trakt-end"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
