"""The stored-title search a tracker's add box (and later a board's) types into
(app/calendar/entries.py's `titles_matching` and `stored_title`).

WHAT IT PROMISES, and each class below holds one of them: one hit per stored
title, marked with every service that stored it; the same hit type a service
search answers with; every stored title whatever calendar or viewer filter it
sits behind; the calendar's own shortest-query rule rather than a second one;
and a title read back BY ITS IDENTITY for a pick, never from the request.

No network: the autouse guard in tests/conftest.py fails any test that reaches
for one, which is exactly the assertion a keystroke path needs.
"""
from __future__ import annotations

import dataclasses
import unittest

from app import db
from app.calendar import entries
from app.calendar import search as calendar_search
from app.providers import title_search
from app.providers.base import ItemKey, Media, Record, Source
from tests.support import migrated_db

AIR = 1783540800.0  # 2026-07-08 20:00Z


def _record(title: str, *, source=Source.TRAKT, media=Media.SHOW, source_id=None,
            tmdb=None, network="", year=2026, anime_type="", **ids) -> Record:
    name = str(source)
    source_id = source_id or f"{name}-{title.lower().replace(' ', '-')}"
    record_ids = {name: source_id, **ids}
    if tmdb is not None:
        record_ids["tmdb"] = tmdb
    return Record(
        source=source, media=media, id=source_id, ids=record_ids,
        detail_url=f"https://{name}.test/x", title=title, air_ts=AIR,
        year=year, network=network, season=1, episode_number=1,
        anime_type=anime_type, enriched=True)


class StoredTitleTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        migrated_db(f"titles-matching-{id(self)}")

    async def store(self, *records, endpoint="shows/premieres"):
        now = db.now()
        await entries.store_loose_airings(endpoint, list(records), now=now,
                                          stale_after=now + 86400)


class OneHitPerTitleTests(StoredTitleTestCase):
    async def test_a_title_two_services_stored_is_one_hit_marked_with_both(self):
        await self.store(
            _record("Frieren: Beyond Journey's End", tmdb=209867, network="Nippon TV",
                    source_id="1001"),
            _record("Sousou no Frieren", source=Source.SIMKL, tmdb=209867,
                    source_id="2002", mal="52991"))
        hits = await entries.titles_matching("frieren", Media.SHOW, 20)
        self.assertEqual(len(hits), 1)
        hit = hits[0]
        self.assertIsInstance(hit, title_search.MergedSearchHit)
        # REGISTRY ORDER LEADS, as it does in the service merge.
        self.assertEqual(list(hit.source_ids), [Source.TRAKT, Source.SIMKL])
        self.assertEqual(hit.title, "Frieren: Beyond Journey's End")
        # The ids union — the whole reason a merge is worth more than either row.
        self.assertEqual(hit.ids["mal"], "52991")
        self.assertEqual(hit.ids["tmdb"], 209867)
        self.assertEqual(hit.key, ItemKey("show", "tmdb", "209867"))
        self.assertEqual(hit.title_key, "show:tmdb:209867")
        self.assertIsNone(hit.season)

    async def test_the_gaps_the_leader_left_are_filled_from_the_next_service(self):
        await self.store(
            _record("Severance", tmdb=95396, network=""),
            _record("Severance", source=Source.SIMKL, tmdb=95396, network="Apple TV+",
                    source_id="3003"))
        hit, = await entries.titles_matching("severance", Media.SHOW, 20)
        self.assertEqual(hit.network, "Apple TV+")

    async def test_the_poster_is_the_stored_one_and_a_missing_one_is_filled(self):
        """The picture the calendar's card draws, from the same row, for free."""
        await self.store(
            _record("Severance", tmdb=95396),
            dataclasses.replace(_record("Severance", source=Source.SIMKL, tmdb=95396,
                                        source_id="3003"),
                                poster="https://simkl.in/posters/12/x_m.jpg"))
        hit, = await entries.titles_matching("severance", Media.SHOW, 20)
        self.assertEqual(hit.poster, "https://simkl.in/posters/12/x_m.jpg")

    async def test_a_title_stored_on_two_calendars_is_still_one_hit(self):
        await self.store(_record("The Rig", tmdb=112581), endpoint="shows/premieres")
        await self.store(_record("The Rig", tmdb=112581), endpoint="shows")
        self.assertEqual(len(await entries.titles_matching("rig", Media.SHOW, 20)), 1)

    async def test_the_simkl_lookup_is_addressed_by_its_number_not_a_slug(self):
        """A Simkl row stored before its storage key became the numeric id is
        filed under a slug, and the per-title lookup a pick makes is addressed by
        number — so the hit calls back with the number from its ids."""
        await self.store(_record("Kanata Kara", source=Source.SIMKL, tmdb=5,
                                 source_id="kanata-kara", simkl=777))
        hit, = await entries.titles_matching("kanata", Media.SHOW, 20)
        self.assertEqual(hit.source_ids, {Source.SIMKL: "777"})

    async def test_a_title_the_waterfall_cannot_key_is_still_listed(self):
        """Under-described, not unfileable: a show pick's own lookup may resolve
        an id the stored row never carried, exactly as for a bare search hit."""
        await self.store(_record("Obscure Anime", source=Source.SIMKL,
                                 source_id="4242", simkl=4242))
        hit, = await entries.titles_matching("obscure", Media.SHOW, 20)
        self.assertIsNone(hit.key)
        self.assertEqual(hit.title_key, "simkl:4242")


class UnfilteredTests(StoredTitleTestCase):
    """"Add a show" is not "what is on my calendar": there is no viewer here at
    all, so there is nothing a viewer's filters or sources could hide."""

    async def test_it_takes_no_viewer_and_no_calendar(self):
        import inspect
        self.assertEqual(list(inspect.signature(entries.titles_matching).parameters),
                         ["query", "media", "limit"])

    async def test_every_calendar_s_titles_are_searched(self):
        await self.store(_record("Premiere Show", tmdb=1), endpoint="shows/premieres")
        await self.store(_record("Finale Show", tmdb=2), endpoint="shows/finales")
        titles = {hit.title for hit in await entries.titles_matching("show", Media.SHOW, 20)}
        self.assertEqual(titles, {"Premiere Show", "Finale Show"})


class MediaTests(StoredTitleTestCase):
    async def test_a_show_search_finds_no_film_and_a_film_search_no_show(self):
        await self.store(_record("Dune", media=Media.MOVIE, tmdb=438631), endpoint="movies")
        await self.store(_record("Dune: Prophecy", tmdb=90228))
        self.assertEqual([h.title for h in await entries.titles_matching("dune", Media.SHOW, 20)],
                         ["Dune: Prophecy"])
        films = await entries.titles_matching("dune", Media.MOVIE, 20)
        self.assertEqual([h.title for h in films], ["Dune"])
        self.assertEqual(films[0].media, Media.MOVIE)

    async def test_a_film_simkl_filed_on_a_series_calendar_is_not_a_show(self):
        """anime_type 'movie' is Simkl's own "this is a film" answer — the same
        rule its search port applies to a service's answer."""
        await self.store(_record("The Last Attack", source=Source.SIMKL, tmdb=1333100,
                                 source_id="2544548", anime_type="movie"))
        self.assertEqual(await entries.titles_matching("last attack", Media.SHOW, 20), [])


class QueryTests(StoredTitleTestCase):
    async def test_the_shortest_query_is_the_calendar_search_s_own_rule(self):
        await self.store(_record("X", tmdb=1), _record("Xy", tmdb=2))
        self.assertEqual(await entries.titles_matching("x", Media.SHOW, 20), [])
        self.assertEqual(len(await entries.titles_matching("xy", Media.SHOW, 20)), 1)
        # ONE RULE: the calendar's search reads the same constant rather than
        # keeping a copy of its own.
        self.assertFalse(hasattr(calendar_search, "MIN_QUERY"))

    async def test_the_rule_counts_folded_characters(self):
        await self.store(_record("Pokémon", tmdb=60572))
        self.assertEqual(len(await entries.titles_matching("  Po ", Media.SHOW, 20)), 1)
        self.assertEqual(len(await entries.titles_matching("POKEMON", Media.SHOW, 20)), 1)

    async def test_a_wildcard_typed_is_a_character(self):
        await self.store(_record("100% Footy", tmdb=1), _record("1000 Ways", tmdb=2))
        self.assertEqual([h.title for h in await entries.titles_matching("100%", Media.SHOW, 20)],
                         ["100% Footy"])

    async def test_a_title_starting_with_the_query_comes_first_then_the_shorter(self):
        await self.store(_record("The Office Ladies", tmdb=1), _record("Office Ladies", tmdb=2),
                         _record("Office", tmdb=3))
        self.assertEqual([h.title for h in await entries.titles_matching("office", Media.SHOW, 20)],
                         ["Office", "Office Ladies", "The Office Ladies"])

    async def test_the_limit_is_a_limit_on_titles(self):
        await self.store(*(_record(f"Show {n}", tmdb=n) for n in range(1, 8)))
        self.assertEqual(len(await entries.titles_matching("show", Media.SHOW, 3)), 3)


class StoredTitleTests(StoredTitleTestCase):
    """How a typed pick is resolved: by the identity it was listed under, read
    back out of the rows the services wrote."""

    async def test_it_answers_the_hit_the_list_drew(self):
        await self.store(
            _record("Clevatess", tmdb=258348, source_id="247331"),
            _record("Clevatess II", source=Source.SIMKL, tmdb=258348, source_id="2505908"))
        listed, = await entries.titles_matching("clevatess", Media.SHOW, 20)
        self.assertEqual(await entries.stored_title(listed.title_key, Media.SHOW), listed)

    async def test_a_key_nothing_stores_answers_none(self):
        self.assertIsNone(await entries.stored_title("show:tmdb:1", Media.SHOW))
        self.assertIsNone(await entries.stored_title("", Media.SHOW))

    async def test_a_key_stored_as_the_other_medium_answers_none(self):
        await self.store(_record("Dune", media=Media.MOVIE, tmdb=438631), endpoint="movies")
        self.assertIsNone(await entries.stored_title("movie:tmdb:438631", Media.SHOW))
        self.assertIsNotNone(await entries.stored_title("movie:tmdb:438631", Media.MOVIE))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
