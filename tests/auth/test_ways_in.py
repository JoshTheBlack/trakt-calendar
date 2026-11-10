"""An account's ways in, and the one function that says what is left without
one of them (app/auth/identities.py's `WaysIn`).

What is held here: the refusal to remove an account's last way in, and the
account page's offer of an Unlink button, are ONE answer — the page draws a
button exactly where the removal would be allowed — and that answer is the rule
it always was: a password or another link an account can sign in with must
remain, and a link nobody signs in with (TMDB's) never counts.
"""
from __future__ import annotations

import asyncio
import itertools

from app import auth, db
from tests.support import AppTestCase

# One link of each kind: two services an account signs in with, and one it
# does not.
LINKS = ("trakt", "simkl", "tmdb")
SIGNS_IN = {"trakt", "simkl"}


def _today(password: bool, links: frozenset[str], removing: str) -> bool:
    """Whether removing `removing` is refused, as the rule has always read."""
    return removing in SIGNS_IN and not password and not (links & SIGNS_IN) - {removing}


class TheOfferIsTheRefusalTests(AppTestCase):

    def account(self, password: bool, links: frozenset[str]) -> int:
        n = next(self._made)
        user = self.make_user(f"ways{n}", calendar_approved=True)
        if not password:
            asyncio.run(db.execute("UPDATE users SET password_hash = NULL WHERE id = ?", (user,)))
        for provider in links:
            self.link_identity(user, provider, 1000 + n * 10 + LINKS.index(provider),
                               access_token=f"{provider}-token")
        return user

    def setUp(self):
        super().setUp()
        self._made = itertools.count()

    def test_the_page_offers_unlink_exactly_where_the_removal_is_allowed(self):
        for password in (True, False):
            for size in range(1, len(LINKS) + 1):
                for links in map(frozenset, itertools.combinations(LINKS, size)):
                    user = self.account(password, links)
                    self.sign_in_as(user)
                    page = self.client.get("/me").text
                    for provider in sorted(links):
                        with self.subTest(password=password, links=sorted(links),
                                          removing=provider):
                            refused = _today(password, links, provider)
                            self.assertEqual(f"unlink-{provider}" in page, not refused)
                            ways = asyncio.run(auth.account_ways_in(user))
                            self.assertEqual(ways.leaves_none(link=provider), refused)
                    # And the removal itself, one at a time on accounts of
                    # their own, refuses exactly those.
                    for provider in sorted(links):
                        other = self.account(password, links)
                        with self.subTest(password=password, links=sorted(links),
                                          removing=provider, removed=True):
                            if _today(password, links, provider):
                                with self.assertRaises(auth.LastLoginMethod):
                                    asyncio.run(auth.unlink_identity(other, provider))
                            else:
                                self.assertTrue(asyncio.run(auth.unlink_identity(other, provider)))

    def test_an_administrator_may_remove_the_last_way_in_deliberately(self):
        user = self.account(False, frozenset({"trakt"}))
        with self.assertRaises(auth.LastLoginMethod):
            asyncio.run(auth.unlink_identity(user, "trakt"))
        self.assertTrue(asyncio.run(auth.unlink_identity(user, "trakt", force=True)))
        self.assertEqual(asyncio.run(auth.account_ways_in(user)),
                         auth.WaysIn(password=False, links=frozenset()))

    def test_a_link_nobody_signs_in_with_is_not_a_way_in(self):
        user = self.account(False, frozenset({"trakt", "tmdb"}))
        ways = asyncio.run(auth.account_ways_in(user))
        self.assertEqual(ways.links, frozenset({"trakt"}))
        self.assertFalse(ways.leaves_none(link="tmdb"))
        self.assertTrue(ways.leaves_none(link="trakt"))
