"""The account page's three tabs (templates/auth_me.html, _account_card.html).

What is held: the server says which tab opens (`?tab=`, else the first); every
section is on the page, inside its tab's panel, whichever is open — so an answer
redrawing one out of band still lands; and the radios are OUTSIDE the card, so
the answer that redraws the whole card (an unlink) leaves the open tab alone.
"""
from __future__ import annotations

import re

from app.auth import routes
from tests.support import AppTestCase

PANELS = {
    "sign-in": ("accountIdentities", "accountPasskeys", "accountCredentials"),
    "profile": ("accountAvatar", "accountNames"),
    "preferences": ("metadataOrder", "shareWatchHistory"),
}


def _panel(html: str, key: str) -> str:
    start = html.index(f'data-account-panel="{key}"')
    following = html.find('data-account-panel="', start + 1)
    return html[start:following if following != -1 else len(html)]


def _checked(html: str) -> list[str]:
    return re.findall(r'id="account-tab-([a-z-]+)" class="account-tab-radio"\s+autocomplete="off" checked',
                      html)


class TheTabsTests(AppTestCase):

    def setUp(self):
        super().setUp()
        self.user_id = self.make_user("tabbed", calendar_approved=True)
        self.sign_in_as(self.user_id)

    def test_a_bare_visit_opens_the_first_tab_and_a_named_one_opens_that(self):
        first = routes.ACCOUNT_TABS[0]["key"]
        self.assertEqual(_checked(self.client.get("/me").text), [first])
        for tab in routes.ACCOUNT_TABS:
            with self.subTest(tab=tab["key"]):
                self.assertEqual(_checked(self.client.get(f"/me?tab={tab['key']}").text),
                                 [tab["key"]])
        self.assertEqual(_checked(self.client.get("/me?tab=nonsense").text), [first])

    def test_every_section_is_on_the_page_in_its_tabs_panel(self):
        html = self.client.get("/me?tab=preferences").text
        for key, sections in PANELS.items():
            panel = _panel(html, key)
            for section in sections:
                with self.subTest(panel=key, section=section):
                    self.assertIn(f'id="{section}"', panel)

    def test_the_radios_are_outside_the_card_an_unlink_redraws(self):
        html = self.client.get("/me").text
        card = html.index('id="accountCard"')
        self.assertTrue(all(html.index(f'id="account-tab-{tab["key"]}"') < card
                            for tab in routes.ACCOUNT_TABS))
        self.link_identity(self.user_id, "trakt", 501, access_token="t")
        answer = self.client.post("/me/identities/unlink", json={"provider": "trakt"}).text
        self.assertIn('id="accountCard"', answer)
        self.assertNotIn("account-tab-radio", answer)
        self.assertIn('for="account-tab-sign-in"', answer)

    def test_an_account_without_a_username_is_pointed_at_profile_from_sign_in(self):
        from app import db
        import asyncio
        asyncio.run(db.execute("UPDATE users SET username = NULL WHERE id = ?", (self.user_id,)))
        html = self.client.get("/me").text
        self.assertIn('for="account-tab-profile">choose one on Profile', _panel(html, "sign-in"))
        self.assertIn('hx-post="/me/username"', _panel(html, "profile"))
