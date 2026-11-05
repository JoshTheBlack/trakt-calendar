"""The line under a calendar card, decided from the title store
(app/metadata/panel.py's `season_lines`).

THE PROMISES:
  - IT READS THE STORE AND ASKS NOBODY: a season a service has described draws
    its count and its days; one the store has not been told about draws nothing
    and names the service still owed it, for the page to queue.
  - IT IS detail_source's SEASON RULE: the describing service first; where the
    store says it holds nothing of the season, each later service in turn — and
    never past a service whose season the store does not know, which asking
    would have reached first.
"""
from __future__ import annotations

from datetime import date

from app import db
from app.config import Settings
from app.metadata import descriptions, panel
from tests.support import DatabaseTestCase

TODAY = date(2026, 9, 3)
# A TMDB read token's shape (three base64url segments), with nothing secret in it.
TMDB_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJyb3V0ZXMifQ.c2lnbmF0dXJl"
SETTINGS = Settings(trakt_client_id="cid", tmdb_api_key=TMDB_TOKEN)
IDS = {"tmdb": 900, "trakt": 77}
AIRED = ["2026-08-18", "2026-08-25", "2026-09-01", "2026-09-08", "2026-09-15"]


def _summary(days: list[str], season: int = 2) -> dict:
    """A service's season summary as a lookup answers it: its total and its days."""
    from app.providers import season as season_rules
    return {"season": season, **season_rules.derive_season(
        [date.fromisoformat(day) for day in days], TODAY)}


class SeasonLinesTests(DatabaseTestCase):

    async def _keep(self, source: str, own_id, days: list[str], season: int = 2) -> None:
        await db.transaction(lambda conn: descriptions.write_season_summary(
            conn, source, "show", own_id, _summary(days, season), ids=IDS, now=db.now()))

    async def _lines(self, *wanted_seasons, ids=IDS, media="show"):
        key = await db.fetch_value("SELECT title_key FROM titles LIMIT 1") or "show:tmdb:900"
        wanted = [panel.SeasonWanted(key, media, ids, season) for season in wanted_seasons]
        return await db.run(lambda conn: panel.season_lines(conn, SETTINGS, wanted, today=TODAY))

    def assertConfigured(self):
        self.assertTrue(SETTINGS.tmdb_configured, "the test's TMDB token reads as unset")

    async def test_a_described_season_draws_its_count_and_its_days(self):
        self.assertConfigured()
        await self._keep("tmdb", 900, AIRED)
        (found,) = await self._lines(2)
        self.assertIsNone(found.owed)
        self.assertEqual((found.line["episode_count"], found.line["last_aired"],
                          found.line["next_aired"]), (5, "2026-09-01", "2026-09-08"))

    async def test_a_season_the_store_does_not_know_draws_nothing_and_names_who_owes_it(self):
        await self._keep("tmdb", 900, AIRED, season=1)
        (found,) = await self._lines(2)
        self.assertIsNone(found.line)
        self.assertEqual((str(found.owed.source), found.owed.source_id), ("tmdb", "900"))

    async def test_a_season_the_first_service_holds_nothing_of_is_the_nexts(self):
        """detail_source.for_season's rule, decided from what the store holds."""
        await self._keep("tmdb", 900, [])
        await self._keep("trakt", 77, AIRED[:2])
        (found,) = await self._lines(2)
        self.assertIsNone(found.owed)
        self.assertEqual(found.line["episode_count"], 2)

    async def test_the_next_service_unknown_decides_nothing(self):
        """Asking would reach Trakt before settling for TMDB's empty answer, so
        a store that does not know Trakt's season cannot say what the line is."""
        await self._keep("tmdb", 900, [])
        (found,) = await self._lines(2)
        self.assertIsNone(found.line)
        self.assertEqual(str(found.owed.source), "trakt")

    async def test_nobody_holding_the_season_draws_nothing_and_owes_nothing(self):
        await self._keep("tmdb", 900, [])
        await self._keep("trakt", 77, [])
        (found,) = await self._lines(2)
        self.assertIsNone(found.owed)
        self.assertFalse(found.line and found.line["episode_count"])

    async def test_a_title_nobody_can_describe_or_a_film_draws_nothing(self):
        (no_ids,) = await self._lines(2, ids={})
        self.assertEqual(no_ids, panel.SeasonLine(None))
        (film,) = await self._lines(1, media="movie")
        self.assertEqual(film, panel.SeasonLine(None))

    async def test_one_read_for_every_season_asked(self):
        """However many cards a page draws, their seasons are one query."""
        await self._keep("tmdb", 900, AIRED)
        await self._keep("tmdb", 900, AIRED[:3], season=3)
        statements: list[str] = []

        def traced(conn):
            conn.set_trace_callback(statements.append)
            try:
                key = conn.execute("SELECT title_key FROM titles LIMIT 1").fetchone()[0]
                statements.clear()
                return panel.season_lines(
                    conn, SETTINGS, [panel.SeasonWanted(key, "show", IDS, season)
                                     for season in (2, 3, 2, 4)], today=TODAY)
            finally:
                conn.set_trace_callback(None)

        found = await db.run(traced)
        self.assertEqual(len(statements), 1)
        self.assertEqual([line.line["episode_count"] if line.line else None for line in found],
                         [5, 3, 5, None])
