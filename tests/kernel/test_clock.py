"""The date override: inert by default, unreachable from a request, and loud.

app/clock.py exists so a month rollover can be watched happening. The value of
the seam is entirely in the guarantees around it, so those are what this file
pins down — that the ordinary case never enters the override path, that nothing a
caller can put in a request reaches it, and that an instance running on a faked
date says so.
"""
from __future__ import annotations

import logging
import os
import unittest
from datetime import date, timedelta
from unittest import mock

from app import clock

from ..support import AppTestCase


def with_fake_today(value: str):
    """Patch just TRAKT_FAKE_TODAY into the environment, leaving the rest alone."""
    return mock.patch.dict(os.environ, {clock.FAKE_TODAY_ENV: value})


def without_fake_today():
    """The environment minus TRAKT_FAKE_TODAY, restored on exit.

    `mock.patch.dict` has no "delete this one key" form, so the whole environment
    is replaced with a copy that omits it — the rest of it (TRAKT_DATA_DIR above
    all) has to survive or nothing in the suite can find its database.
    """
    return mock.patch.dict(
        os.environ,
        {k: v for k, v in os.environ.items() if k != clock.FAKE_TODAY_ENV},
        clear=True)


class ClockDefaultsToTheRealDateTests(unittest.TestCase):
    """Unset variable -> the real clock, and no override path taken at all."""

    def test_override_is_none_when_the_variable_is_absent(self):
        with without_fake_today():
            self.assertIsNone(clock.override())

    def test_today_is_the_real_date_when_the_variable_is_absent(self):
        with without_fake_today():
            self.assertEqual(clock.today(), date.today())

    def test_an_empty_variable_is_the_same_as_an_absent_one(self):
        # An operator clearing the value by blanking it rather than unsetting it
        # must get the real clock, not a parse failure.
        with with_fake_today("   "):
            self.assertIsNone(clock.override())

    def test_the_override_is_read_live_so_unsetting_it_takes_effect(self):
        # Captured-at-import would leave a process stuck on a date its environment
        # no longer names, which is the failure that is hardest to diagnose.
        with with_fake_today("2001-02-03"):
            self.assertEqual(clock.today(), date(2001, 2, 3))
        with without_fake_today():
            self.assertEqual(clock.today(), date.today())


class ClockRefusesUnusableValuesTests(unittest.TestCase):
    """A malformed value falls back to the real clock and says why."""

    def test_a_non_date_is_ignored(self):
        with with_fake_today("tomorrow"), self.assertLogs("app.clock", logging.WARNING):
            self.assertIsNone(clock.override())

    def test_an_impossible_date_is_ignored(self):
        with with_fake_today("2026-02-30"), self.assertLogs("app.clock", logging.WARNING):
            self.assertIsNone(clock.override())

    def test_a_bad_value_does_not_stop_today_answering(self):
        # The seam must never be the reason a process cannot serve a request.
        with with_fake_today("2026-13-01"), self.assertLogs("app.clock", logging.WARNING):
            self.assertEqual(clock.today(), date.today())


class ClockAnnouncesItselfTests(unittest.TestCase):
    """An instance on a fake clock must say so at boot, every boot."""

    def test_the_warning_names_the_faked_date(self):
        with with_fake_today("2030-06-01"):
            with self.assertLogs("app.clock", logging.WARNING) as caught:
                clock.warn_if_overridden()
        self.assertIn("2030-06-01", "\n".join(caught.output))

    def test_the_warning_names_the_variable_so_it_can_be_removed(self):
        with with_fake_today("2030-06-01"):
            with self.assertLogs("app.clock", logging.WARNING) as caught:
                clock.warn_if_overridden()
        self.assertIn(clock.FAKE_TODAY_ENV, "\n".join(caught.output))

    def test_it_is_silent_on_the_real_clock(self):
        # assertLogs fails when nothing is logged, so this is the way to assert
        # the absence of a line without depending on a caplog fixture.
        with without_fake_today():
            with self.assertRaises(AssertionError):
                with self.assertLogs("app.clock", logging.WARNING):
                    clock.warn_if_overridden()


class NoRequestCanMoveTheClockTests(AppTestCase):
    """THE SECURITY PROPERTY: the date is not addressable from the outside.

    A clock a caller can move is a way to unfreeze a month the tracker has closed
    or re-run a rollover, so it matters that the only writer is whoever starts the
    process. These drive real requests carrying every shape a caller controls —
    query string, header, cookie — and assert the app's answer does not move.
    """

    def _today_the_app_believes(self) -> date:
        # Read through the app's own seam rather than a page's rendered text, so
        # this stays true regardless of how any particular template formats a date.
        return clock.today()

    def test_a_query_parameter_named_after_the_variable_is_inert(self):
        real = self._today_the_app_believes()
        self.client.get(f"/healthz?{clock.FAKE_TODAY_ENV}=2035-01-01")
        self.client.get("/healthz?today=2035-01-01")
        self.assertEqual(self._today_the_app_believes(), real)

    def test_a_header_named_after_the_variable_is_inert(self):
        real = self._today_the_app_believes()
        self.client.get("/healthz", headers={
            clock.FAKE_TODAY_ENV: "2035-01-01",
            "X-Fake-Today": "2035-01-01",
        })
        self.assertEqual(self._today_the_app_believes(), real)

    def test_a_cookie_named_after_the_variable_is_inert(self):
        real = self._today_the_app_believes()
        self.client.cookies.set(clock.FAKE_TODAY_ENV, "2035-01-01")
        self.client.get("/healthz")
        self.assertEqual(self._today_the_app_believes(), real)

    def test_the_app_never_writes_the_variable_itself(self):
        # The guarantee is "the process environment is the only writer". Nothing
        # under app/ may assign to os.environ at all, so a request cannot reach it
        # through some route that sets a variable as a side effect.
        from pathlib import Path

        app_dir = Path(clock.__file__).parent
        offenders = [
            f"{path.relative_to(app_dir)}:{n}"
            for path in app_dir.rglob("*.py")
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if "os.environ[" in line or "os.environ.setdefault" in line
            or "environ.update" in line
        ]
        self.assertEqual(offenders, [], "app/ writes to the process environment")


class TheOverrideActuallyMovesThePageTests(AppTestCase):
    """The seam is worthless if it does not reach the request path.

    The inertness tests above would all pass on a variable nothing reads, so one
    test has to show the other direction: with the variable set, a request-path
    caller sees the faked date.
    """

    def test_a_date_far_from_now_reaches_a_request_path_reader(self):
        from app.distrakt import store

        far = date.today() + timedelta(days=400)
        with with_fake_today(far.isoformat()):
            self.assertEqual(clock.today(), far)
            # store.month_standing defaults its `today` from the same seam, and it
            # is what decides whether a month may still be edited — the single
            # most rollover-relevant reader in the app.
            self.assertEqual(
                store.month_standing(store.month_key(far.year, far.month)),
                store.MonthStanding.CURRENT)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
