"""Naming the background job an outbound call belongs to.

Every outbound call logs one netGET line, and on a busy heartbeat those
interleave: season lookups, a title enrichment and a calendar refill land in the
same second looking identical apart from their paths. Reading a path back to the
job that wanted it is guesswork, and it is guesswork exactly when something is
looping and somebody is trying to work out which loop.

No network, no database: this is a contextvar and a decorator.
"""
from __future__ import annotations

import asyncio
import unittest

from app import perftrace


class ActivityTagTests(unittest.TestCase):
    def test_nothing_is_tagged_outside_a_job(self):
        """A request path has a request to attribute to already, so a suffix
        there would be noise on every line the app logs."""
        self.assertEqual(perftrace.activity_tag(), "")

    def test_a_job_names_itself(self):
        with perftrace.activity("episode lookup"):
            self.assertEqual(perftrace.activity_tag(), "  [episode lookup]")
        self.assertEqual(perftrace.activity_tag(), "")

    def test_a_nested_job_restores_the_outer_one(self):
        """Clearing rather than restoring would leave the outer job anonymous for
        the rest of its own pass — the half of a drain AFTER it called something
        else, which is the half a reader is usually looking at."""
        with perftrace.activity("outer"):
            with perftrace.activity("inner"):
                self.assertEqual(perftrace.activity_tag(), "  [inner]")
            self.assertEqual(perftrace.activity_tag(), "  [outer]")


class JobDecoratorTests(unittest.TestCase):
    def test_the_decorator_labels_every_caller(self):
        """On the function rather than the call site: a drain is called from the
        heartbeat, from a fill that has just stored records, and from tests, and
        a label attached at one of those is missing from the other two."""
        seen = []

        @perftrace.job("film releases")
        async def drain():
            seen.append(perftrace.activity_tag())
            return 7

        self.assertEqual(asyncio.run(drain()), 7)
        self.assertEqual(seen, ["  [film releases]"])
        self.assertEqual(perftrace.activity_tag(), "")

    def test_the_label_does_not_survive_an_exception(self):
        @perftrace.job("title enrichment")
        async def boom():
            raise RuntimeError("the source refused")

        with self.assertRaises(RuntimeError):
            asyncio.run(boom())
        self.assertEqual(perftrace.activity_tag(), "")

    def test_it_keeps_the_wrapped_name(self):
        """Otherwise every drain in a traceback reads `wrapper`."""
        @perftrace.job("episode lookup")
        async def drain_episodes():
            return None

        self.assertEqual(drain_episodes.__name__, "drain_episodes")


if __name__ == "__main__":
    unittest.main()
