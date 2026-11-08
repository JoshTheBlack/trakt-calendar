"""app/db.py's decision made before a write's turn (`decide`, `transaction`'s
`decided`).

THE PROMISES: a decision is used in the turn only when every read it was made
from returns the same rows there — then the turn does not decide again; a write
between the two that changed what it read sends the turn to decide again, and
the body acts on that; a write it did not read changes nothing; and a decision
is never handed a turn without the `prepare` it came from.
"""
from __future__ import annotations

import asyncio
import threading
from unittest import mock

from app import db
from tests.support import DatabaseTestCase


class ADecisionMadeBeforeItsTurnTests(DatabaseTestCase):

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await db.execute("INSERT INTO app_meta (key, value) VALUES ('a', '1'), ('b', '1')")
        self.decided_on: list[str] = []

    def _prepare(self, conn) -> str:
        """Reads 'a' twice, the second through fetchone, as a lazy reader would."""
        (value,) = conn.execute("SELECT value FROM app_meta WHERE key = 'a'").fetchone()
        again = conn.execute("SELECT value FROM app_meta WHERE key = ?", ("a",)).fetchall()
        self.decided_on.append(value)
        return f"{value}/{again[0][0]}"

    async def _turn(self, decided) -> str:
        written: list[str] = []

        def body(conn, prepared):
            written.append(prepared)
            conn.execute("UPDATE app_meta SET value = ? WHERE key = 'b'", (prepared,))
        await db.transaction(body, prepare=self._prepare, decided=decided)
        return written[0]

    async def test_reads_that_read_the_same_use_the_decision(self):
        decided = await db.decide(self._prepare)
        self.assertEqual(decided.value, "1/1")
        self.assertEqual(len(decided.reads), 2)
        self.assertEqual(await self._turn(decided), "1/1")
        self.assertEqual(self.decided_on, ["1"], "decided once, before the turn")

    async def test_a_write_to_what_it_read_decides_again(self):
        decided = await db.decide(self._prepare)
        await db.execute("UPDATE app_meta SET value = '2' WHERE key = 'a'")
        self.assertEqual(await self._turn(decided), "2/2")
        self.assertEqual(self.decided_on, ["1", "2"])
        self.assertEqual(await db.fetch_value("SELECT value FROM app_meta WHERE key = 'b'"), "2/2")

    async def test_a_write_to_what_it_did_not_read_changes_nothing(self):
        decided = await db.decide(self._prepare)
        await db.execute("INSERT INTO app_meta (key, value) VALUES ('c', '9')")
        self.assertEqual(await self._turn(decided), "1/1")
        self.assertEqual(self.decided_on, ["1"])

    async def test_a_decision_always_stale_is_decided_in_its_turn_at_last(self):
        """Decided again outside the queue REDECIDE_OUTSIDE times, then once in
        the turn, which is what the body acts on."""
        decided = await db.decide(self._prepare)
        with mock.patch.object(db, "_reads_the_same", lambda conn, reads: False):
            self.assertEqual(await self._turn(decided), "1/1")
        self.assertEqual(len(self.decided_on), 1 + db.REDECIDE_OUTSIDE + 1)

    async def test_a_write_does_not_wait_while_a_stale_decision_is_made_again(self):
        """The turn that found its decision stale is given up; deciding again
        happens with the queue free for everybody else."""
        decided = await db.decide(self._prepare)
        await db.execute("UPDATE app_meta SET value = '2' WHERE key = 'a'")
        deciding, release = threading.Event(), threading.Event()
        real = self._prepare

        def slow(conn):
            deciding.set()
            release.wait(5)
            return real(conn)

        self._prepare = slow
        turn = asyncio.ensure_future(self._turn(decided))
        self.assertTrue(await asyncio.to_thread(deciding.wait, 5))
        try:
            await asyncio.wait_for(
                db.execute("UPDATE users SET timezone = timezone WHERE id = 0"), 2)
        finally:
            release.set()
            self.assertEqual(await turn, "2/2")

    async def test_a_decision_needs_the_prepare_it_came_from(self):
        decided = await db.decide(self._prepare)
        with self.assertRaises(ValueError):
            await db.transaction(lambda conn: None, decided=decided)

    async def test_the_recording_hands_rows_out_as_a_cursor_does(self):
        def prepare(conn):
            cursor = conn.execute("SELECT key FROM app_meta WHERE key IN ('a', 'b') ORDER BY key")
            first = cursor.fetchone()[0]
            rest = [row[0] for row in cursor]
            empty = conn.execute("SELECT key FROM app_meta WHERE key = 'none'").fetchone()
            return first, rest, empty, conn.in_transaction
        decided = await db.decide(prepare)
        self.assertEqual(decided.value, ("a", ["b"], None, False))
