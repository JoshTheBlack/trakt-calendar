"""The rules every page in the account, admin and sign-in family owes now that
each is markup that posts itself, asserted across all of them at once.

Each rule has bitten before somewhere else, which is why it is a test and not a
habit: a control posting without the encoder is refused by the shape guard and
reads as a press that did nothing; a read inside a writing form inherits the
encoder and is dropped; an out-of-band selector with a space in it swaps nothing,
silently; and a page script is the thing these pages stopped needing.
"""
from __future__ import annotations

import asyncio
import re
import unittest

from app import auth
from app.auth import encryption_flow
from tests.support import AppTestCase


class AccountFamilyTestCase(AppTestCase):
    def setUp(self):
        super().setUp()
        self.admin_id = self.make_user("admin", is_admin=True, calendar_approved=True)
        self.member_id = self.make_user("member", calendar_approved=True)
        self.link_identity(self.member_id, "trakt", 1)
        self.link_identity(self.member_id, "simkl", 2)
        self.invite = asyncio.run(auth.create_invite(created_by=self.admin_id))["token"]

    def pages(self) -> dict[str, str]:
        """Every page in the family, as the account that would see its controls."""
        rendered = {}
        self.sign_in_as(self.admin_id)
        rendered["/admin"] = self.client.get("/admin").text
        self.sign_in_as(self.member_id)
        rendered["/me"] = self.client.get("/me").text
        self.client.cookies.clear()
        rendered["/login"] = self.client.get("/login").text
        rendered["/register"] = self.client.get(f"/register?invite={self.invite}").text
        encryption_flow._health = encryption_flow.KEY_MISSING
        self.addCleanup(setattr, encryption_flow, "_health", encryption_flow.HEALTHY)
        self.sign_in_as(self.admin_id)
        rendered["/admin/encryption/recovery"] = self.client.get(
            "/admin/encryption/recovery", headers={"Accept": "text/html"}).text
        return rendered


class TheMarkupWritesThroughTheEncoderTests(AccountFamilyTestCase):
    def test_every_control_that_writes_carries_the_encoder(self):
        for page, html in self.pages().items():
            for element in re.findall(r"<(?:form|button|input|select|span|div)\b[^>]*hx-post[^>]*>", html):
                with self.subTest(page=page, element=element[:80]):
                    self.assertIn("hx-json-enc", element)

    def test_a_read_inside_a_writing_form_opts_out_of_the_encoder(self):
        for page, html in self.pages().items():
            for form in re.findall(r"<form\b[^>]*hx-json-enc[^>]*>(.*?)</form>", html, re.S):
                for element in re.findall(r"<[a-z]+\b[^>]*hx-get[^>]*>", form):
                    with self.subTest(page=page, element=element[:80]):
                        self.assertIn('hx-json-enc="false"', element)

    def test_every_writing_form_opts_out_of_boosting(self):
        for page, html in self.pages().items():
            for form in re.findall(r"<form\b[^>]*hx-post[^>]*>", html):
                with self.subTest(page=page, form=form[:80]):
                    self.assertIn('hx-boost="false"', form)

    def test_no_out_of_band_selector_has_a_space_in_it(self):
        """htmx splits the value into a swap style and a selector only when there
        is no space, so `outerHTML:#a .b` swaps nothing at all."""
        self.sign_in_as(self.member_id)
        answers = [
            self.client.post("/me/display-name", json={"display_name": "Mem"}).text,
            self.client.post("/me/share-watch-history", json={"enabled": "0"}).text,
        ]
        for html in [*self.pages().values(), *answers]:
            for value in re.findall(r'hx-swap-oob="([^"]*)"', html):
                with self.subTest(value=value):
                    self.assertNotIn(" ", value)

    def test_no_page_in_the_family_carries_a_script_of_its_own(self):
        """What is left in these pages' <head> is the shared one: htmx, the encoder,
        the loader's map, and the tracker reveal the site header owns."""
        for page, html in self.pages().items():
            body = html.split("</head>", 1)[1]
            with self.subTest(page=page):
                self.assertNotRegex(body, r"<script\b(?![^>]*type=\"application/json\")")
                self.assertNotRegex(body, r"\son[a-z]+=\"")
                for gone in ("confirmInline", "prompt(", "alert(", "plexAuth", "__authEnterHandler",
                             "htmx:response:error"):
                    self.assertNotIn(gone, html)


# Every write the account page makes, with a body that would otherwise be accepted.
ACCOUNT_POSTS = (
    ("/me/display-name", {"display_name": "Name"}),
    ("/me/username", {"username": "claimed"}),
    ("/me/password", {"password": "long-enough-password", "password_confirm": "long-enough-password"}),
    ("/me/share-watch-history", {"enabled": "0"}),
    ("/me/order/metadata_order", {"source": "simkl", "direction": "up"}),
    ("/me/identities/unlink", {"provider": "trakt"}),
    ("/me/avatar", {"image_b64": ""}),
    ("/me/avatar/remove", {}),
    ("/me/avatar/use", {"source": "uploaded"}),
    ("/me/avatar/refresh", {"source": "plex"}),
    ("/me/avatar/forget", {"source": "plex"}),
)


class AccountRefusalTests(AccountFamilyTestCase):
    """Each account write refused once for each way it can be. A bad VALUE is
    drawn in the write's own section and is covered beside each write's rule in
    test_routes.py; these are the refusals that never reach one."""

    def setUp(self):
        super().setUp()
        self.sign_in_as(self.member_id)

    def unchanged(self):
        row = asyncio.run(auth.get_user(self.member_id))
        self.assertIsNone(row["display_name"])
        self.assertEqual(row["username"], "member")
        self.assertEqual(len(asyncio.run(auth.list_identities(self.member_id))), 2)


    def test_a_body_that_is_not_an_object_is_refused(self):
        for path, _ in ACCOUNT_POSTS:
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, json=["x"]).status_code, 400)
        self.unchanged()

    def test_no_session_is_refused(self):
        self.client.cookies.clear()
        for path, body in ACCOUNT_POSTS:
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, json=body).status_code, 401)
        self.unchanged()

if __name__ == "__main__":
    unittest.main()
