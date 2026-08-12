"""When two services describe the SAME AIRING, and when they only look like it.

NAMED FOR THE CONCERN RATHER THAN FOR ONE MODULE, because the concern genuinely
spans two: the rule itself lives in app/calendar/cache.py's `match_keys`, and
the id bridge it depends on is a batched read of the enrichment table that
app/calendar/enrich.py owns. Splitting these across two files would put the
"should this merge" cases in one and the "could it even see the id" cases in
another, and every one of them is the same question.

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

from app import db
from app.calendar import cache as calendar_cache, enrich as calendar_enrich
from app.providers.base import Record, Source
from tests.support import new_db_path

# The instants below are the real ones. 2026-07-06T21:17:00Z and 21:20:00Z are
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


def keys(records):
    return calendar_cache.match_keys(records)


def cards(records):
    """How many separate cards these records produce — the thing a viewer
    counts."""
    return len(calendar_cache.group_records(records))


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
        self.assertEqual(keys(pair), ["show:tmdb:309974|1|1"] * 2)
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
                self.assertEqual(len(set(keys(pair))), 1)


class OneAiringDescribedAtTwoResolutionsTests(unittest.TestCase):
    """The same id, the same day, and still two cards — because one service
    stated coordinates and the other did not.

    All four titles below were confirmed rendering as two cards on the author's
    calendar before this existed. Note the shape: Simkl states an EPISODE NUMBER
    and no season at all, which is the ordinary spelling for an anime airing, so
    it is not "no coordinates" so much as half of one.
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
        self.assertEqual(keys(pair), ["show:tmdb:284581|1|1"] * 2)
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

    def test_eight_uncoordinated_records_on_one_day_each_find_their_own_episode(self):
        """THE CASE THAT SETTLES WHY THE STATED EPISODE NUMBER IS PART OF THE
        RULE. One live window holds Star Wars: Visions eight times from Simkl —
        every episode dropping on one day, none with a season — against Trakt's
        eight coordinated ones. Matching on the day alone makes all eight
        ambiguous and merges none of them; the episode number picks each out.
        Before this, the eight Simkl records collided on one key and were stored
        under the repeat-airing suffixes #2 through #8.
        """
        records = ([simkl("Star Wars: Visions Presents - The Ninth Jedi",
                          {"simkl": 3173584, "tmdb": "289324"}, air_ts=AUG04, episode=n)
                    for n in range(1, 9)]
                   + [trakt("Star Wars: Visions Presents - The Ninth Jedi",
                            {"trakt": 282931, "tmdb": 289324}, air_ts=AUG04, season=1, episode=n)
                      for n in range(1, 9)])
        self.assertEqual(cards(records), 8)
        self.assertEqual(sorted(set(keys(records))),
                         [f"show:tmdb:289324|1|{n}" for n in range(1, 9)])
        for group in calendar_cache.group_records(records):
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
        self.assertEqual(sorted(keys(pair)),
                         ["show:tmdb:152261|4|1", "show:tmdb:152261|5|1"])

    def test_celebrity_family_feud_seasons_12_and_13_stay_apart(self):
        self.assertEqual(cards(self.pair("Celebrity Family Feud", 82108, 12, 13, 1783641600.0)), 2)

    def test_hard_knocks_seasons_21_and_27_stay_apart(self):
        self.assertEqual(cards(self.pair("Hard Knocks", 12940, 21, 27, AUG05)), 2)

    def test_an_uncoordinated_record_refuses_rather_than_picking_a_season(self):
        """The dangerous version of the same shape: a third record that states
        neither season, beside two seasons that both could be it. Two candidates
        is not one, so it keeps its own key and draws its own card — the failure
        direction that shows a duplicate instead of hiding a title behind the
        wrong one.

        AND IT IS THE CASE THAT PINS THE COUNTING ORDER. One of the two seasons
        came from this record's own source, so a rule that dropped that one first
        and counted afterwards would find a single survivor — the OTHER season —
        and merge a season 4 listing into a season 5 card."""
        records = self.pair("Drag Race France", 152261, 5, 4, JUL08)
        records.append(simkl("Drag Race France", {"simkl": 9, "tmdb": "152261"}, air_ts=JUL08))
        self.assertEqual(keys(records)[2], "show:tmdb:152261")
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
        self.assertEqual(keys([left, right]), ["movie:tmdb:550"] * 2)
        self.assertEqual(cards([left, right]), 1)


class TheIdBridgeAtFillTests(unittest.IsolatedAsyncioTestCase):
    """One title, two id spaces that share nothing, and the row that joins them.

    The Elusive Samurai is listed by Simkl as `mal:60059` with no tmdb anywhere in
    the calendar file, and by Trakt as `tmdb:222623` S02E01 — one airing, on one
    day, that no waterfall over the two payloads can bring together. Simkl's own
    catalog record for it carries BOTH ids, so the bridge exists; what it needed
    was for the fill to go and ask, since the key is derived at fill and the
    read-time overlay arrives after it.
    """

    async def asyncSetUp(self):
        new_db_path("matching")
        await db.migrate()

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
        self.assertEqual(sorted(keys(records)),
                         ["show:mal:60059", "show:tmdb:222623|2|1"])

    async def test_the_stored_catalog_row_brings_them_onto_one_key(self):
        await self.store_catalog_row()
        records = await calendar_enrich.overlay_match_ids(self.records())
        self.assertEqual(keys(records), ["show:tmdb:222623|2|1"] * 2)
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


class AWindowStoredUnderTheOlderRuleTests(unittest.TestCase):
    """A stored window is keyed under whatever the matcher said when it was
    filled, so the rule moving means the stored version moves with it."""

    def test_the_version_moved(self):
        self.assertEqual(calendar_cache.PAYLOAD_VERSION, 3)

    def test_a_window_from_the_previous_rule_reads_as_a_miss(self):
        """Not an error and not a partial: a miss, so it is refetched and re-keyed
        under the current rule. A month spans five or six windows, and a mixture
        would show one airing merged in a refilled window and split in a stale
        one, on the same page."""
        stale = calendar_cache._compress(
            {"v": 2, "sources": ["trakt"], "asked": ["trakt"],
             "entries": [{"key": "show:tmdb:1|1|1", "ids": {}, "by_source": {}}]})
        self.assertIsNone(calendar_cache._decompress(stale))
