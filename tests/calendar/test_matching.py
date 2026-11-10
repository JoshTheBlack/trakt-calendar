"""When two services describe the SAME AIRING, and when they only look like it.

NAMED FOR THE CONCERN RATHER THAN FOR ONE MODULE, because the concern genuinely
spans three: which episode a listing is is decided as it is written
(app/metadata/episodes.py's filing and fold), how one episode's listings are
drawn as cards is read back by app/calendar/cache.py's `cards_of`, and the id
bridge both depend on is a batched read of the enrichment table that
app/calendar/enrich.py owns. Splitting these would put the "should this merge"
cases in one file and the "could it even see the id" cases in another, and every
one of them is the same question.

EVERY CASE BELOW IS TRANSCRIBED FROM THE AUTHOR'S OWN STORED WINDOWS, ids and
coordinates and air instants included, read out of the live cache read-only on
2026-08-06. That is deliberate: a matcher tested only against invented records
agrees with whatever it was written to do, and the three things that actually
make this hard — a source that states an episode and no season, two seasons of
one show premiering on one day, and one title keyed in two id spaces that share
nothing — are all shapes nobody would have invented.
"""
from __future__ import annotations

import unittest
from datetime import date

from app import db
from app.calendar import cache as calendar_cache, enrich as calendar_enrich, entries
from app.calendar import query as calendar_query
from app.providers.base import Record, Source
from tests import support
from tests.support import as_stored, migrated_db, stored_groups

# The instants below are the real ones. 2026-07-06T21:17:00Z and 21:20:00Z are


async def _stored_span(endpoint_key, first, after):
    """Every listing of each episode row with one in [first, after), whole —
    the rows a span's reader works from."""
    from app import db
    from app.calendar import query as _query

    return await db.run(lambda conn: support._stored(
        conn, endpoint_key, _query.read_rows(conn, endpoint_key, first=first, after=after)))

# what the two services published for the same premiere — three minutes apart,
# which is why "the same instant" could never have been the test and the UTC day
# is.
JUL06_SIMKL = 1783339020.0
JUL06_TRAKT = 1783339200.0
JUL08 = 1783530000.0
JUL17 = 1784298600.0
AUG04 = 1785866400.0
AUG05 = 1785891600.0


def simkl(title, ids, *, air_ts, season=None, episode=None):
    # enriched=False because that is what Simkl's own normalizer produces — the
    # calendar CDN files carry none of the fields it stands for.
    return Record(source=Source.SIMKL, media="show", id=str(ids.get("simkl") or title),
                  ids=dict(ids), detail_url="https://simkl.com/x", title=title,
                  air_ts=air_ts, season=season, episode_number=episode, enriched=False)


def trakt(title, ids, *, air_ts, season=None, episode=None):
    return Record(source=Source.TRAKT, media="show", id=str(ids.get("slug") or title),
                  ids=dict(ids), detail_url="https://trakt.tv/x", title=title,
                  air_ts=air_ts, season=season, episode_number=episode)


def rows(records):
    """The episode row the store files each record on, in order."""
    return [record.card for record in as_stored(records)]


def cards(records):
    """How many separate cards these records produce — the thing a viewer
    counts."""
    return len(stored_groups(records))


class TheIdTypesMustNotMakeTwoTitlesTests(unittest.TestCase):
    """Simkl reports a tmdb id as a string, Trakt as an int, and the two are the
    same title.

    THIS IS THE FAILURE THAT LOOKS EXACTLY LIKE SPARSE COVERAGE, which is the
    only reason it gets its own class: a matcher comparing the raw values matches
    nothing at all, every card renders twice, and the symptom reads as "Simkl
    just does not have these" rather than as a bug. The values are the real ones
    off the live cache.
    """

    def test_a_string_id_and_an_int_id_are_one_title(self):
        pair = [trakt("Love Unseen Beneath the Clear Night Sky",
                      {"trakt": 312888, "tmdb": 309974, "imdb": "tt39304754"},
                      air_ts=JUL06_TRAKT, season=1, episode=1),
                simkl("Love Unseen Beneath the Clear Night Sky",
                      {"simkl": 2934514, "tmdb": "309974", "mal": "62936"},
                      air_ts=JUL06_TRAKT, season=1, episode=1)]
        self.assertEqual(rows(pair), ["show:tmdb:309974|S1E1"] * 2)
        self.assertEqual(cards(pair), 1)

    def test_the_coercion_holds_wherever_the_waterfall_lands(self):
        """Not a tmdb special case: the same string/int split on any id space the
        waterfall can reach has to read the same way, or the bug simply moves to
        whichever id a given pair happens to share."""
        for namespace, left, right in (("tmdb", 309974, "309974"),
                                       ("tvdb", 470520, "470520"),
                                       ("mal", 62936, "62936")):
            with self.subTest(namespace=namespace):
                pair = [trakt("T", {namespace: left}, air_ts=JUL06_TRAKT, season=1, episode=1),
                        simkl("T", {namespace: right}, air_ts=JUL06_TRAKT, season=1, episode=1)]
                self.assertEqual(len(set(rows(pair))), 1)


class OneAiringDescribedAtTwoResolutionsTests(unittest.TestCase):
    """The same id, the same day, and still two cards — because one service
    stated coordinates and the other did not.

    All four titles below were confirmed rendering as two cards on the author's
    calendar before the fold existed. Note the shape: Simkl states an EPISODE
    NUMBER and no season at all, which is the ordinary spelling for an anime
    airing, so it is not "no coordinates" so much as half of one.
    """

    def pair(self, title, tmdb, simkl_id, *, episode, season, day_simkl, day_trakt):
        return [simkl(title, {"simkl": simkl_id, "tmdb": str(tmdb)},
                      air_ts=day_simkl, episode=episode),
                trakt(title, {"trakt": 1, "tmdb": tmdb},
                      air_ts=day_trakt, season=season, episode=episode)]

    def test_the_insipid_prince_becomes_one_card(self):
        pair = self.pair("The Insipid Prince's Furtive Grab for the Throne",
                         284581, 2263626, episode=1, season=1,
                         day_simkl=JUL06_SIMKL, day_trakt=JUL06_TRAKT)
        self.assertEqual(rows(pair), ["show:tmdb:284581|S1E1"] * 2)
        self.assertEqual(cards(pair), 1)

    def test_love_unseen_becomes_one_card(self):
        pair = self.pair("Love Unseen Beneath the Clear Night Sky",
                         309974, 2934514, episode=1, season=1,
                         day_simkl=JUL06_TRAKT, day_trakt=JUL06_TRAKT)
        self.assertEqual(cards(pair), 1)

    def test_the_ghost_in_the_shell_becomes_one_card(self):
        pair = self.pair("The Ghost in the Shell", 255358, 2474924, episode=1, season=1,
                         day_simkl=1783432800.0, day_trakt=1783432800.0)
        self.assertEqual(cards(pair), 1)

    def test_star_wars_visions_becomes_one_card(self):
        pair = self.pair("Star Wars: Visions Presents - The Ninth Jedi",
                         289324, 3173584, episode=1, season=1,
                         day_simkl=AUG04, day_trakt=AUG04)
        self.assertEqual(cards(pair), 1)

    def test_the_listing_still_says_what_it_stated(self):
        """FOLDED, NOT REWRITTEN: the listing sits on the numbered episode's row
        and still reads back stating no season, which is what lets it leave that
        row again when the numbered listing does (`TheFoldIsDecidedAtWriteTests`)
        and keeps the card's coordinate the most complete service's."""
        pair = self.pair("Love Unseen Beneath the Clear Night Sky",
                         309974, 2934514, episode=1, season=1,
                         day_simkl=JUL06_TRAKT, day_trakt=JUL06_TRAKT)
        (group,) = stored_groups(pair)
        self.assertIsNone(group["by_source"]["simkl"].season)
        self.assertEqual(group["by_source"]["trakt"].season, 1)

    def test_eight_uncoordinated_records_on_one_day_each_find_their_own_episode(self):
        """THE CASE THAT SETTLES WHY THE STATED EPISODE NUMBER IS PART OF THE
        RULE. One live window holds Star Wars: Visions eight times from Simkl —
        every episode dropping on one day, none with a season — against Trakt's
        eight coordinated ones. Matching on the day alone makes all eight
        ambiguous and merges none of them; the episode number picks each out.
        """
        records = ([simkl("Star Wars: Visions Presents - The Ninth Jedi",
                          {"simkl": 3173584, "tmdb": "289324"}, air_ts=AUG04, episode=n)
                    for n in range(1, 9)]
                   + [trakt("Star Wars: Visions Presents - The Ninth Jedi",
                            {"trakt": 282931, "tmdb": 289324}, air_ts=AUG04, season=1, episode=n)
                      for n in range(1, 9)])
        self.assertEqual(cards(records), 8)
        self.assertEqual(sorted(set(rows(records))),
                         sorted(f"show:tmdb:289324|S1E{n}" for n in range(1, 9)))
        for group in stored_groups(records):
            self.assertEqual(sorted(group["by_source"]), ["simkl", "trakt"])


class TwoSeasonsOnOneDayMustStayTwoCardsTests(unittest.TestCase):
    """The three pairs a naive matcher destroys.

    Two different seasons of one show premiering on the same day is a real thing,
    all three of these were confirmed on the page, and every one of them has the
    same title, the same tmdb id, the same day and the same instant. They survive
    because both sides state a FULL coordinate, so neither is a candidate for
    being folded into anything.
    """

    def pair(self, title, tmdb, left_season, right_season, air_ts):
        return [trakt(title, {"trakt": 1, "tmdb": tmdb}, air_ts=air_ts,
                      season=left_season, episode=1),
                simkl(title, {"simkl": 2, "tmdb": str(tmdb)}, air_ts=air_ts,
                      season=right_season, episode=1)]

    def test_drag_race_france_seasons_4_and_5_stay_apart(self):
        pair = self.pair("Drag Race France", 152261, 5, 4, JUL08)
        self.assertEqual(cards(pair), 2)
        self.assertEqual(sorted(rows(pair)),
                         ["show:tmdb:152261|S4E1", "show:tmdb:152261|S5E1"])

    def test_celebrity_family_feud_seasons_12_and_13_stay_apart(self):
        self.assertEqual(cards(self.pair("Celebrity Family Feud", 82108, 12, 13, 1783641600.0)), 2)

    def test_hard_knocks_seasons_21_and_27_stay_apart(self):
        self.assertEqual(cards(self.pair("Hard Knocks", 12940, 21, 27, AUG05)), 2)

    def test_an_uncoordinated_record_refuses_rather_than_picking_a_season(self):
        """The dangerous version of the same shape: a third record that states
        neither season, beside two seasons that both could be it. Two candidates
        is not one, so it keeps its own row and draws its own card — the failure
        direction that shows a duplicate instead of hiding a title behind the
        wrong one.

        AND IT IS THE CASE THAT PINS THE COUNTING ORDER. One of the two seasons
        came from this record's own source, so a rule that dropped that one first
        and counted afterwards would find a single survivor — the OTHER season —
        and merge a season 4 listing into a season 5 card."""
        records = self.pair("Drag Race France", 152261, 5, 4, JUL08)
        records.append(simkl("Drag Race France", {"simkl": 9, "tmdb": "152261"}, air_ts=JUL08))
        self.assertEqual(rows(records)[2], "show:tmdb:152261|D2026-07-08")
        self.assertEqual(cards(records), 3)


class WhatTheMatcherRefusesTests(unittest.TestCase):
    """The conditions that are not optional, each stated as the merge it
    prevents."""

    def test_a_different_day_is_a_different_airing(self):
        """A show airing twice in one week is two airings, and an uncoordinated
        record naming neither has no way to say which it means."""
        records = [trakt("Weekly", {"tmdb": 5}, air_ts=JUL06_TRAKT, season=1, episode=1),
                   simkl("Weekly", {"simkl": 6, "tmdb": "5"}, air_ts=JUL08)]
        self.assertEqual(cards(records), 2)

    def test_one_service_listing_an_airing_twice_is_not_a_merge(self):
        """Folding a source's own uncoordinated record into its own coordinated
        one would be collapsing that service's listing rather than reconciling
        two of them — and a service listing one airing twice is a repeat the
        calendar has always drawn twice."""
        records = [simkl("Repeat", {"simkl": 6, "tmdb": "5"}, air_ts=JUL06_TRAKT,
                         season=1, episode=1),
                   simkl("Repeat", {"simkl": 6, "tmdb": "5"}, air_ts=JUL06_TRAKT)]
        self.assertEqual(cards(records), 2)

    def test_a_stated_episode_that_disagrees_blocks_the_merge(self):
        records = [trakt("Show", {"tmdb": 5}, air_ts=JUL06_TRAKT, season=1, episode=1),
                   simkl("Show", {"simkl": 6, "tmdb": "5"}, air_ts=JUL06_TRAKT, episode=4)]
        self.assertEqual(cards(records), 2)

    def test_a_stated_season_that_disagrees_blocks_the_merge(self):
        """The other half of the same rule: a few Trakt records state a season
        and no episode number, so the half that IS stated has to be honoured in
        both directions."""
        records = [simkl("Show", {"simkl": 6, "tmdb": "5"}, air_ts=JUL06_TRAKT,
                         season=2, episode=1),
                   trakt("Show", {"tmdb": 5}, air_ts=JUL06_TRAKT, season=1)]
        self.assertEqual(cards(records), 2)

    def test_a_title_no_shared_id_space_names_never_merges(self):
        """A per-source key can never collide with anything, which is what makes
        an unmatchable title a visible extra card rather than a wrong merge."""
        records = [trakt("Untitled", {"trakt": 1}, air_ts=JUL06_TRAKT, season=1, episode=1),
                   simkl("Untitled", {"simkl": 2}, air_ts=JUL06_TRAKT)]
        self.assertEqual(cards(records), 2)

    def test_a_film_is_keyed_on_the_title_alone(self):
        """Movies carry no coordinates at all and are not episodics that lost
        them, so nothing here should reach for one."""
        left = Record(source=Source.TRAKT, media="movie", id="f", ids={"tmdb": 550},
                      detail_url="", title="Film", air_ts=JUL06_TRAKT, date_only=True)
        right = Record(source=Source.SIMKL, media="movie", id="g", ids={"tmdb": "550"},
                       detail_url="", title="Film", air_ts=JUL06_TRAKT, date_only=True)
        self.assertEqual(rows([left, right]), ["movie:tmdb:550|film"] * 2)
        self.assertEqual(cards([left, right]), 1)


class TheIdBridgeAtFillTests(unittest.IsolatedAsyncioTestCase):
    """One title, two id spaces that share nothing, and the row that joins them.

    The Elusive Samurai is listed by Simkl as `mal:60059` with no tmdb anywhere in
    the calendar file, and by Trakt as `tmdb:222623` S02E01 — one airing, on one
    day, that no waterfall over the two payloads can bring together. Simkl's own
    catalog record for it carries BOTH ids, so the bridge exists; what it needed
    was for the fill to go and ask, since the title is decided at fill and the
    read-time overlay arrives after it.
    """

    async def asyncSetUp(self):
        migrated_db("matching")

    async def asyncTearDown(self):
        db.close_thread_connection()

    def records(self):
        return [simkl("The Elusive Samurai", {"simkl": 2601798, "mal": "60059"},
                      air_ts=JUL17, episode=1),
                trakt("The Elusive Samurai",
                      {"trakt": 203330, "tmdb": 222623, "tvdb": 432455, "imdb": "tt27187054"},
                      air_ts=JUL17, season=2, episode=1)]

    async def store_catalog_row(self):
        """The enrichment row exactly as the live table holds it for this title,
        ids and all."""
        await calendar_enrich._upsert_success(
            2601798, "show",
            {"extract_version": 2,
             "ids": {"simkl": "2601798", "mal": "60059", "anidb": "18903",
                     "imdb": "tt27187054", "tmdb": "222623", "tvdb": "432455"}},
            1786000000)

    async def test_without_the_row_it_is_honestly_two_cards(self):
        """The before-state, asserted rather than assumed: nothing in the two
        payloads can join them, so two cards is the correct answer until
        something else knows better."""
        records = await calendar_enrich.overlay_match_ids(self.records())
        self.assertEqual(cards(records), 2)
        self.assertEqual(sorted(rows(records)),
                         ["show:mal:60059|D2026-07-17E1", "show:tmdb:222623|S2E1"])

    async def test_the_stored_catalog_row_brings_them_onto_one_key(self):
        await self.store_catalog_row()
        records = await calendar_enrich.overlay_match_ids(self.records())
        self.assertEqual(rows(records), ["show:tmdb:222623|S2E1"] * 2)
        self.assertEqual(cards(records), 1)

    async def test_the_bridge_takes_the_ids_and_nothing_else(self):
        """Everything else enrichment holds stays a READ-time overlay: baking
        genres or a network into a stored window would freeze one moment's
        enrichment into a row served for a whole TTL, and would make `enriched`
        a claim about the window that is not true of it."""
        await calendar_enrich._upsert_success(
            2601798, "show",
            {"extract_version": 2, "ids": {"tmdb": "222623"},
             "genres": ["anime"], "network": "NTV", "country": "jp"},
            1786000000)
        record = (await calendar_enrich.overlay_match_ids(self.records()))[0]
        self.assertEqual(record.ids["tmdb"], "222623")
        self.assertEqual(record.genres, [])
        self.assertEqual(record.network, "")
        self.assertFalse(record.enriched)

    async def test_the_calendar_files_own_id_wins_over_the_catalogs(self):
        """First writer wins, the same rule the group's hoisted `ids` uses: the
        calendar file is what was published for THIS airing, and the catalog
        record describes the title."""
        await calendar_enrich._upsert_success(
            2601798, "show", {"extract_version": 2, "ids": {"mal": "999999"}}, 1786000000)
        record = (await calendar_enrich.overlay_match_ids(self.records()))[0]
        self.assertEqual(record.ids["mal"], "60059")

    async def test_a_trakt_record_is_never_touched(self):
        """This table is keyed on a Simkl id and holds Simkl's catalog; there is
        nothing in it that could be about another source's record."""
        await self.store_catalog_row()
        before = dict(self.records()[1].ids)
        record = (await calendar_enrich.overlay_match_ids(self.records()))[1]
        self.assertEqual(record.ids, before)


class TheFoldIsDecidedAtWriteTests(unittest.IsolatedAsyncioTestCase):
    """WHICH EPISODE A LISTING IS, IS DECIDED AS IT IS WRITTEN, AND RE-DECIDED
    BY EVERY WRITE THAT TOUCHES ITS TITLE AND DAY — both ways. A numbered
    listing arriving takes a listing that names no full episode onto its row; a
    refill that no longer lists the numbered one lets it go back to its own day,
    which is what a read deciding it each time used to draw."""

    WEEK = date(2026, 7, 13)
    DAY = 1784131200.0  # 2026-07-15

    async def asyncSetUp(self):
        migrated_db("foldatwrite")

    async def asyncTearDown(self):
        db.close_thread_connection()

    def loose(self):
        return simkl("Anime", {"simkl": 2, "tmdb": "9"}, air_ts=self.DAY + 3600, episode=1)

    def numbered(self):
        return trakt("Anime", {"trakt": 1, "tmdb": 9}, air_ts=self.DAY, season=1, episode=1)

    async def store(self, records, sources):
        await calendar_cache.store_window("shows", self.WEEK, records, 600, 1000,
                                          sources=sources)

    async def cards(self):
        records = await _stored_span("shows", self.WEEK, date(2026, 7, 20))
        return sorted(tuple(sorted(group["by_source"]))
                      for group in support.grouped(records))

    async def test_a_numbered_listing_arriving_takes_it_in(self):
        await self.store([self.loose()], ["simkl"])
        self.assertEqual(await self.cards(), [("simkl",)])
        await self.store([self.numbered()], ["trakt"])
        self.assertEqual(await self.cards(), [("simkl", "trakt")])

    async def test_a_numbered_listing_leaving_lets_it_go_back(self):
        await self.store([self.loose(), self.numbered()], ["simkl", "trakt"])
        self.assertEqual(await self.cards(), [("simkl", "trakt")])
        await self.store([], ["trakt"])
        self.assertEqual(await self.cards(), [("simkl",)])
        records = await _stored_span("shows", self.WEEK, date(2026, 7, 20))
        self.assertEqual([(r.season, r.episode_number, r.card) for r in records],
                         [(None, 1, "show:tmdb:9|D2026-07-15E1")])


# Dexter: Original Sin's premiere as the two services stored it on the owner's
# instance: Simkl's per-title lookup gives the day (Sunday 15 December, pinned at
# UTC midnight), Trakt the instant (10pm Eastern that Sunday, which is Monday
# 03:00 UTC) — and the storage windows start on Mondays.
DEC15_SIMKL_DAY = 1734220800.0
DEC16_TRAKT_0300Z = 1734318000.0
DAY = 86400


def _utc(year, month, day, hour=0):
    from datetime import datetime, timezone

    return datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp()


class TheJoinRuleTests(unittest.TestCase):
    """`may_join`: whether one service's repeated listing and another service's
    listing of ONE episode are drawn as one card — the same calendar month, or at
    most a week apart across a month's edge."""

    def test_the_rule_as_a_table(self):
        for first, second, joins, why in (
            (date(2026, 11, 21), date(2026, 11, 23), True, "Yellowjackets S04E01"),
            (date(2026, 11, 1), date(2026, 11, 21), True, "20 days, one month"),
            (date(2026, 11, 1), date(2026, 11, 30), True, "a whole month apart inside it"),
            (date(2026, 10, 28), date(2026, 11, 3), True, "6 days across an edge"),
            (date(2026, 10, 25), date(2026, 11, 1), True, "7 days across an edge"),
            (date(2026, 10, 24), date(2026, 11, 1), False, "8 days across an edge"),
            (date(2026, 12, 28), date(2027, 1, 3), True, "across a year's edge"),
            (date(2026, 9, 16), date(2026, 11, 16), False, "two months apart"),
            (date(2026, 4, 8), date(2026, 9, 16), False, "Dimension 20 S29E1"),
        ):
            with self.subTest(why):
                self.assertEqual(calendar_query.may_join(first, second), joins)
                self.assertEqual(calendar_query.may_join(second, first), joins)


class OneEpisodeIsOneCardTests(unittest.TestCase):
    """`cards_of` and the groups built from it: every service's listing of one
    episode is one card whatever dates they give it, and only one service
    listing it twice draws it twice."""

    def yellowjackets(self, *, simkl_ts=None, trakt_ts=None):
        return [simkl("Yellowjackets", {"simkl": 1, "tmdb": "117488"},
                      air_ts=simkl_ts or _utc(2026, 11, 21, 1), season=4, episode=1),
                trakt("Yellowjackets", {"trakt": 2, "tmdb": 117488},
                      air_ts=trakt_ts or _utc(2026, 11, 23, 2), season=4, episode=1)]

    def test_yellowjackets_s04e01_is_one_card_across_a_monday(self):
        """THE REPORTED CASE. Simkl dates it Saturday 01:00 UTC and Trakt the
        following Monday 02:00 UTC — two storage windows, two days apart."""
        listings = self.yellowjackets()
        self.assertNotEqual(
            *(calendar_cache.window_start(calendar_cache.record_utc_date(r)) for r in listings))
        groups = stored_groups(listings)
        self.assertEqual(len(groups), 1)
        self.assertEqual(sorted(groups[0]["by_source"]), ["simkl", "trakt"])
        self.assertEqual(groups[0]["key"], "show:tmdb:117488|S4E1#1")
        self.assertEqual(groups[0]["base"], "show:tmdb:117488")

    def test_dexters_premiere_across_a_windows_edge_is_one_card(self):
        listings = [
            Record(source=Source.SIMKL, media="show", id="2091057",
                   ids={"simkl": "2091057", "tmdb": "219937"}, detail_url="",
                   title="Dexter: Original Sin", air_ts=DEC15_SIMKL_DAY, date_only=True,
                   season=1, episode_number=1, enriched=False),
            trakt("Dexter: Original Sin", {"trakt": 202335, "tmdb": 219937},
                  air_ts=DEC16_TRAKT_0300Z, season=1, episode=1)]
        group, = stored_groups(listings)
        self.assertEqual(group["ids"]["simkl"], "2091057")
        self.assertEqual(group["ids"]["trakt"], 202335)

    def test_a_same_month_pair_20_days_apart_is_one_card(self):
        listings = self.yellowjackets(simkl_ts=_utc(2026, 11, 1, 1),
                                      trakt_ts=_utc(2026, 11, 21, 2))
        self.assertEqual(cards(listings), 1)

    def test_a_month_boundary_pair_6_days_apart_is_one_card(self):
        listings = self.yellowjackets(simkl_ts=_utc(2026, 10, 28, 1),
                                      trakt_ts=_utc(2026, 11, 3, 2))
        self.assertEqual(cards(listings), 1)

    def test_two_services_far_apart_are_still_one_card(self):
        """ONE EPISODE, ONE CARD, WHATEVER THE DATES: two services disagreeing
        about which day an episode airs — eight days across a month's edge, two
        months, Dimension 20's April against September — are one episode said
        twice, drawn once on the viewer's own service's day with the other day
        beside it (app/calendar/resolve.py)."""
        for simkl_ts, trakt_ts in ((_utc(2026, 10, 24, 1), _utc(2026, 11, 1, 2)),
                                   (_utc(2026, 9, 16, 23), _utc(2026, 11, 16, 23)),
                                   (_utc(2026, 4, 8), _utc(2026, 9, 16))):
            with self.subTest(simkl_ts=simkl_ts):
                self.assertEqual(cards(self.yellowjackets(simkl_ts=simkl_ts,
                                                          trakt_ts=trakt_ts)), 1)

    def test_one_service_listing_an_episode_twice_stays_two(self):
        """Its own two listings: joining would collapse a service's listing
        rather than reconcile two services."""
        first = trakt("Show", {"trakt": 1, "tmdb": 5}, air_ts=_utc(2026, 11, 2),
                      season=1, episode=1)
        second = trakt("Show", {"trakt": 1, "tmdb": 5}, air_ts=_utc(2026, 11, 9),
                       season=1, episode=1)
        self.assertEqual(cards([first, second]), 2)

    def test_a_repeat_keeps_the_listing_it_was_drawn_with(self):
        """WHERE ONE SERVICE LISTS AN EPISODE TWICE, the other service's listing
        is drawn with the repeat it may join — the same month, or a week across
        an edge — and not with whichever came first: Simkl lists a film on 14
        September and again on 6 November, TMDB on 6 November."""
        simkl_early = Record(source=Source.SIMKL, media="movie", id="3141969",
                             ids={"simkl": 3141969, "tmdb": "1707841"}, detail_url="",
                             title="Glass Saints", air_ts=_utc(2026, 9, 14), date_only=True)
        simkl_late = Record(source=Source.SIMKL, media="movie", id="3141969",
                            ids={"simkl": 3141969, "tmdb": "1707841"}, detail_url="",
                            title="Glass Saints", air_ts=_utc(2026, 11, 6), date_only=True)
        tmdb = Record(source=Source.TMDB, media="movie", id="1707841", ids={"tmdb": 1707841},
                      detail_url="", title="Glass Saints", air_ts=_utc(2026, 11, 6),
                      date_only=True)
        groups = stored_groups([simkl_early, simkl_late, tmdb])
        self.assertEqual(sorted((sorted(g["by_source"]), g["by_source"]["simkl"].air_ts)
                                for g in groups),
                         [(["simkl"], _utc(2026, 9, 14)),
                          (["simkl", "tmdb"], _utc(2026, 11, 6))])

    def test_a_third_service_far_from_both_still_joins(self):
        """Trakt 28 October and Simkl 2 November are drawn together; a third
        service's 29 November is a month from Trakt's — and still the same
        episode, so the same card."""
        listings = [
            trakt("Show", {"trakt": 1, "tmdb": 5}, air_ts=_utc(2026, 10, 28),
                  season=1, episode=1),
            simkl("Show", {"simkl": 2, "tmdb": "5"}, air_ts=_utc(2026, 11, 2),
                  season=1, episode=1),
            Record(source=Source.TMDB, media="show", id="5", ids={"tmdb": 5},
                   detail_url="", title="Show", air_ts=_utc(2026, 11, 29),
                   date_only=True, season=1, episode_number=1)]
        self.assertEqual([sorted(str(r.source) for r in card)
                          for card in calendar_query.cards_of(as_stored(listings))],
                         [["simkl", "tmdb", "trakt"]])

    def test_the_cards_do_not_depend_on_the_order_they_were_read_in(self):
        """A month, a day and a search read the same rows in different orders;
        they have to make the same cards out of them."""
        listings = as_stored(self.yellowjackets() + [
            Record(source=Source.TMDB, media="show", id="117488", ids={"tmdb": 117488},
                   detail_url="", title="Yellowjackets", air_ts=_utc(2026, 11, 22),
                   date_only=True, season=4, episode_number=1)])
        forward = calendar_query.cards_of(listings)
        backward = calendar_query.cards_of(list(reversed(listings)))
        self.assertEqual([[str(r.source) for r in card] for card in forward],
                         [[str(r.source) for r in card] for card in backward])
        self.assertEqual(len(forward), 1)

    def test_a_film_is_one_card_on_the_film_calendar(self):
        """A film's release is the one airing it has, so its listings join by
        title alone — measured: Coyote vs. Acme, 20 and 28 August, drew twice."""
        left = Record(source=Source.TRAKT, media="movie", id="f", ids={"tmdb": 550},
                      detail_url="", title="Film", air_ts=_utc(2026, 8, 20), date_only=True)
        right = Record(source=Source.SIMKL, media="movie", id="g", ids={"tmdb": "550"},
                       detail_url="", title="Film", air_ts=_utc(2026, 8, 28), date_only=True)
        self.assertEqual(cards([left, right]), 1)

    def test_an_uncoordinated_listing_still_folds_only_on_its_own_day(self):
        """The same-day fold is not widened: an anime episode Simkl states as
        "episode 1" on a day Trakt does not list S01E01 stays its own card, even
        though two NUMBERED listings join whatever their dates."""
        coordinated = trakt("Anime", {"trakt": 1, "tmdb": 9}, air_ts=_utc(2026, 11, 2),
                            season=1, episode=1)
        loose = simkl("Anime", {"simkl": 2, "tmdb": "9"}, air_ts=_utc(2026, 11, 3), episode=1)
        self.assertEqual(cards([coordinated, loose]), 2)
        same_day = simkl("Anime", {"simkl": 2, "tmdb": "9"}, air_ts=_utc(2026, 11, 2, 5),
                         episode=1)
        self.assertEqual(cards([coordinated, same_day]), 1)


if __name__ == "__main__":
    unittest.main()
