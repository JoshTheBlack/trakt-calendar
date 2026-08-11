"""Provider picture slots: how they are written, adopted, refreshed and removed.

THE RULE THIS FILE EXISTS FOR: a slot may be overwritten freely — it is our copy
of somebody else's picture and a stale one is simply wrong — but `avatar.webp` is
set automatically ONLY when the account has none. After that it changes because
the person chose it, and never because they signed in again.
"""
from __future__ import annotations

import asyncio
import re
import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

from app import auth
from app.auth import provider_avatars, provider_login
from app.media import user_images
from tests.support import AppTestCase


def _png(colour) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (64, 64), colour).save(buf, format="PNG")
    return buf.getvalue()


RED = _png((220, 30, 40))
BLUE = _png((30, 60, 220))


class SlotStorageTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.user_id = self.make_user("slots")
        # The database is fresh per test but DATA_DIR/user_data is not, and
        # make_user hands out the same id each time — so without this a slot
        # written by one test is still on disk for the next one. Cleaned at BOTH
        # ends: the teardown is what stops these tests leaking an avatar into
        # other files' fixtures, which is not hypothetical — a stray avatar.webp
        # here changed what a share card rendered three packages away.
        user_images.delete_user_data(self.user_id)
        self.addCleanup(user_images.delete_user_data, self.user_id)

    def test_a_slot_name_outside_the_closed_set_is_refused_not_joined_to_a_path(self):
        """The path safety here is the closed set, so it is asserted directly
        rather than inferred from a caller behaving well."""
        for bad in ("../../etc/passwd", "trakt/../..", "", "TRAKT", "unknown"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    user_images.provider_avatar_path(self.user_id, bad)

    def test_a_provider_picture_goes_through_the_same_validation_as_an_upload(self):
        """Not a faster path for bytes from a service "we trust". The stored file
        is the normalized 512x512 WebP master, not what the provider served."""
        asyncio.run(user_images.save_provider_avatar(self.user_id, "trakt", RED))
        stored = user_images.provider_avatar_path(self.user_id, "trakt").read_bytes()
        self.assertNotEqual(stored, RED)
        image = Image.open(BytesIO(stored))
        self.assertEqual(image.format, "WEBP")
        self.assertEqual(image.size, (user_images.MASTER_SIZE, user_images.MASTER_SIZE))

    def test_bytes_that_are_not_an_image_are_refused(self):
        with self.assertRaises(user_images.ValidationError):
            asyncio.run(user_images.save_provider_avatar(self.user_id, "trakt", b"not an image"))

    def test_a_slot_is_overwritten_freely_but_the_avatar_is_not(self):
        asyncio.run(user_images.save_provider_avatar(self.user_id, "trakt", RED))
        user_images.adopt_avatar_source(self.user_id, "trakt", only_if_missing=True)
        avatar_before = user_images.avatar_path(self.user_id).read_bytes()

        asyncio.run(user_images.save_provider_avatar(self.user_id, "trakt", BLUE))
        slot_now = user_images.provider_avatar_path(self.user_id, "trakt").read_bytes()
        self.assertNotEqual(slot_now, avatar_before)  # the slot moved on
        # The avatar did NOT follow it: a refresh updates the service's picture,
        # not the account's choice of face.
        self.assertEqual(user_images.avatar_path(self.user_id).read_bytes(), avatar_before)

    def test_an_existing_avatar_is_never_replaced_by_seeding(self):
        """Asserted on the BYTES rather than on a return value: "the endpoint
        said no" and "the file is untouched" are different claims and only the
        second is the one that matters."""
        asyncio.run(user_images.save_avatar(self.user_id, _b64(RED)))
        mine = user_images.avatar_path(self.user_id).read_bytes()
        asyncio.run(user_images.save_provider_avatar(self.user_id, "plex", BLUE))

        self.assertFalse(
            user_images.adopt_avatar_source(self.user_id, "plex", only_if_missing=True))
        self.assertEqual(user_images.avatar_path(self.user_id).read_bytes(), mine)

    def test_choosing_one_explicitly_does_replace_it(self):
        asyncio.run(user_images.save_avatar(self.user_id, _b64(RED)))
        asyncio.run(user_images.save_provider_avatar(self.user_id, "plex", BLUE))
        self.assertTrue(
            user_images.adopt_avatar_source(self.user_id, "plex", only_if_missing=False))
        self.assertTrue(user_images.avatar_source_is_adopted(self.user_id, "plex"))

    def test_removing_a_slot_takes_the_avatar_only_when_it_was_that_copy(self):
        asyncio.run(user_images.save_provider_avatar(self.user_id, "trakt", RED))
        asyncio.run(user_images.save_provider_avatar(self.user_id, "plex", BLUE))
        user_images.adopt_avatar_source(self.user_id, "trakt", only_if_missing=True)

        # Removing the OTHER service's slot leaves the avatar alone.
        user_images.delete_avatar_source(self.user_id, "plex")
        self.assertTrue(user_images.has_avatar(self.user_id))

        # Removing the one being worn takes it with it.
        user_images.delete_avatar_source(self.user_id, "trakt")
        self.assertFalse(user_images.has_avatar(self.user_id))

    def test_slots_do_not_consume_the_saved_image_quota(self):
        """A person's five uploads are theirs and are not spent by linking a
        service."""
        for _ in range(user_images.MAX_IMAGES_PER_USER):
            asyncio.run(user_images.add_image(self.user_id, _b64(RED)))
        for name in ("trakt", "plex", "simkl"):
            asyncio.run(user_images.save_provider_avatar(self.user_id, name, BLUE))
        self.assertEqual(len(user_images.list_images(self.user_id)),
                         user_images.MAX_IMAGES_PER_USER)
        self.assertEqual(len(user_images.list_provider_avatars(self.user_id)), 3)

    def test_deleting_the_account_removes_the_slots_too(self):
        asyncio.run(user_images.save_provider_avatar(self.user_id, "trakt", RED))
        user_images.delete_user_data(self.user_id)
        self.assertEqual(user_images.list_provider_avatars(self.user_id), [])


class SeedingTests(AppTestCase):
    """provider_login.seed_provider_avatar — the step that runs on registration
    and on link, and on nothing else."""

    def setUp(self):
        super().setUp()
        self.user_id = self.make_user("seeded")
        user_images.delete_user_data(self.user_id)  # see the note in SlotStorageTests
        self.addCleanup(user_images.delete_user_data, self.user_id)

    def _identity(self, provider="trakt", url="https://media.trakt.tv/a.png"):
        return auth.ProviderIdentity(provider=provider, provider_user_id="1",
                                     display_name="x", avatar_url=url)

    def test_a_seeded_picture_fills_the_slot_and_an_empty_avatar(self):
        with patch.object(provider_avatars, "fetch", AsyncMock(return_value=RED)):
            asyncio.run(provider_login.seed_provider_avatar(self.user_id, self._identity()))
        self.assertEqual(user_images.list_provider_avatars(self.user_id), ["trakt"])
        self.assertTrue(user_images.avatar_source_is_adopted(self.user_id, "trakt"))

    def test_a_provider_with_no_picture_is_a_no_op_and_does_not_raise(self):
        with patch.object(provider_avatars, "fetch", AsyncMock(return_value=None)):
            asyncio.run(provider_login.seed_provider_avatar(
                self.user_id, self._identity(url=None)))
        self.assertEqual(user_images.list_provider_avatars(self.user_id), [])
        self.assertFalse(user_images.has_avatar(self.user_id))

    def test_bytes_that_fail_validation_never_fail_the_sign_in(self):
        """A picture that arrives and turns out not to be an image is the case
        most likely to throw, and a sign-in must survive it."""
        with patch.object(provider_avatars, "fetch", AsyncMock(return_value=b"nope")):
            asyncio.run(provider_login.seed_provider_avatar(self.user_id, self._identity()))
        self.assertEqual(user_images.list_provider_avatars(self.user_id), [])

    def test_a_fetch_that_explodes_never_fails_the_sign_in(self):
        with patch.object(provider_avatars, "fetch",
                          AsyncMock(side_effect=RuntimeError("CDN on fire"))):
            with self.assertRaises(RuntimeError):
                # The guard is around the STORE, not the fetch — fetch already
                # swallows everything it can see, so a raise here would be a bug
                # in fetch rather than something seed should mask. Pinned so the
                # split stays deliberate.
                asyncio.run(provider_login.seed_provider_avatar(self.user_id, self._identity()))

    def test_seeding_a_second_service_does_not_take_over_the_avatar(self):
        with patch.object(provider_avatars, "fetch", AsyncMock(return_value=RED)):
            asyncio.run(provider_login.seed_provider_avatar(self.user_id, self._identity()))
        first = user_images.avatar_path(self.user_id).read_bytes()
        with patch.object(provider_avatars, "fetch", AsyncMock(return_value=BLUE)):
            asyncio.run(provider_login.seed_provider_avatar(
                self.user_id, self._identity(provider="plex")))
        self.assertEqual(user_images.avatar_path(self.user_id).read_bytes(), first)
        self.assertEqual(sorted(user_images.list_provider_avatars(self.user_id)),
                         ["plex", "trakt"])


def _b64(raw: bytes) -> str:
    import base64
    return base64.b64encode(raw).decode("ascii")


if __name__ == "__main__":
    unittest.main()


class AccountPageAndExportSurfaceTests(AppTestCase):
    """The two things that were built server-side and invisible in the browser.
    Both were reported from real use, and both are asserted here on the surface
    a person actually touches rather than on the helper underneath it."""

    def setUp(self):
        super().setUp()
        self.user_id = self.make_user("surfaces")
        user_images.delete_user_data(self.user_id)
        self.addCleanup(user_images.delete_user_data, self.user_id)
        self.sign_in_as(self.user_id)
        asyncio.run(user_images.save_provider_avatar(self.user_id, "plex", RED))

    def test_using_a_slot_changes_what_the_page_asks_for(self):
        """THE DEFECT: adopting worked, and the page went on drawing the old
        picture because these images are served `private, max-age=86400` and the
        URL either side of the change is identical. The version token is what
        makes the reload ask for something the browser has not got."""
        before = self.client.get("/me").text
        resp = self.client.post("/api/me/avatar/source", json={"source": "plex"})
        self.assertEqual(resp.status_code, 200)
        after = self.client.get("/me").text

        self.assertIn("/api/me/avatar?size=96", after)
        version = re.search(r"/api/me/avatar\?size=96&amp;v=(\d+)", after)
        self.assertIsNotNone(version, "the avatar URL carries no version token")
        # The page did not merely re-render the same address it had before.
        self.assertNotEqual(before, after)

    def test_an_unknown_slot_is_refused_rather_than_joined_to_a_path(self):
        for bad in ("../../etc/passwd", "unknown", ""):
            with self.subTest(bad=bad):
                resp = self.client.post("/api/me/avatar/source", json={"source": bad})
                self.assertEqual(resp.status_code, 400)

    def test_the_export_picker_is_told_which_service_pictures_exist(self):
        """THE OTHER DEFECT: the server accepted a provider header spec and
        nothing ever offered one, because the picker builds its options from
        this payload."""
        data = self.client.get("/api/me/images").json()
        self.assertEqual(data["provider_avatars"], ["plex"])
        # Beside the saved images rather than among them: `max` caps uploads and
        # a provider picture is not one.
        self.assertEqual(data["images"], [])
        self.assertEqual(data["max"], user_images.MAX_IMAGES_PER_USER)


class SigningInDoesNotReseedTests(AppTestCase):
    """THE RULE: a slot is filled when an account is CREATED with a service or
    LINKS one, and refreshed only when somebody presses refresh. An ordinary
    sign-in does neither.

    Two things depend on it and both were briefly broken by seeding beside the
    registration branch rather than inside it: every login would have paid for an
    outbound image fetch to replace a picture that almost never changes, and an
    avatar its owner deleted would have come back the next time they signed in,
    because deleting it is precisely what makes `only_if_missing` true again.
    """

    def setUp(self):
        super().setUp()
        self.user_id = self.make_user("returning")
        user_images.delete_user_data(self.user_id)
        self.addCleanup(user_images.delete_user_data, self.user_id)

    def _identity(self):
        return auth.ProviderIdentity(provider="trakt", provider_user_id="1",
                                     display_name="x",
                                     avatar_url="https://media.trakt.tv/a.png")

    def _sign_in(self, kind):
        """Drive the shared completion seam with a stubbed outcome, which is the
        only part of a provider sign-in this rule depends on."""
        outcome = auth.LoginOutcome(kind, self.user_id, True)
        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="1.2.3.4"),
                                  cookies={}, url=SimpleNamespace(scheme="https"))
        with patch.object(auth, "find_identity", AsyncMock(return_value=object())), \
             patch.object(auth, "login_with_provider_identity", AsyncMock(return_value=outcome)), \
             patch.object(auth, "create_session", AsyncMock(return_value="sid")), \
             patch.object(auth, "client_ip", lambda *a, **k: "1.2.3.4"), \
             patch.object(provider_avatars, "fetch", AsyncMock(return_value=RED)) as fetch:
            asyncio.run(provider_login.complete_provider_login(
                identity=self._identity(), handshake={"invite_token": None},
                request=request, settings=SimpleNamespace(), already_linked="x"))
        return fetch

    def test_an_ordinary_sign_in_fetches_nothing(self):
        fetch = self._sign_in("login")
        fetch.assert_not_awaited()
        self.assertEqual(user_images.list_provider_avatars(self.user_id), [])

    def test_a_registration_does_seed(self):
        fetch = self._sign_in("registered")
        fetch.assert_awaited()
        self.assertEqual(user_images.list_provider_avatars(self.user_id), ["trakt"])

    def test_an_avatar_the_owner_deleted_stays_deleted_across_a_sign_in(self):
        self._sign_in("registered")
        self.assertTrue(user_images.has_avatar(self.user_id))
        user_images.delete_avatar(self.user_id)

        self._sign_in("login")
        self.assertFalse(user_images.has_avatar(self.user_id),
                         "signing in brought back an avatar its owner deleted")


class YourOwnPictureSurvivesTests(AppTestCase):
    """THE SEQUENCE REPORTED FROM REAL USE: upload a picture, wear a service's
    for a while, then remove that service's — and get your own back rather than
    nothing. Adopting is a copy, so without somewhere for the upload to live it
    was destroying the original the first time somebody tried a service's."""

    def setUp(self):
        super().setUp()
        self.user_id = self.make_user("mine")
        user_images.delete_user_data(self.user_id)
        self.addCleanup(user_images.delete_user_data, self.user_id)

    def test_removing_a_worn_slot_restores_the_uploaded_picture(self):
        asyncio.run(user_images.save_avatar(self.user_id, _b64(RED)))
        mine = user_images.avatar_path(self.user_id).read_bytes()
        asyncio.run(user_images.save_provider_avatar(self.user_id, "trakt", BLUE))
        user_images.adopt_avatar_source(self.user_id, "trakt", only_if_missing=False)
        self.assertNotEqual(user_images.avatar_path(self.user_id).read_bytes(), mine)

        user_images.delete_avatar_source(self.user_id, "trakt")
        self.assertEqual(user_images.avatar_path(self.user_id).read_bytes(), mine)

    def test_unlinking_a_service_restores_it_too(self):
        """Same rule through the other door — unlink removes the slot as well."""
        asyncio.run(user_images.save_avatar(self.user_id, _b64(RED)))
        mine = user_images.avatar_path(self.user_id).read_bytes()
        asyncio.run(user_images.save_provider_avatar(self.user_id, "plex", BLUE))
        user_images.adopt_avatar_source(self.user_id, "plex", only_if_missing=False)

        user_images.delete_avatar_source(self.user_id, "plex")
        self.assertEqual(user_images.avatar_path(self.user_id).read_bytes(), mine)

    def test_with_no_upload_behind_it_the_avatar_still_goes(self):
        """An account whose only picture ever came from a service has nothing to
        fall back to, and must not keep wearing a service it has disconnected."""
        asyncio.run(user_images.save_provider_avatar(self.user_id, "trakt", BLUE))
        user_images.adopt_avatar_source(self.user_id, "trakt", only_if_missing=True)
        user_images.delete_avatar_source(self.user_id, "trakt")
        self.assertFalse(user_images.has_avatar(self.user_id))

    def test_removing_your_picture_removes_it_for_good(self):
        """Deleting the avatar deletes the upload behind it, so it cannot
        reappear later when a service's slot is removed — that would be the app
        producing a picture its owner had already deleted."""
        asyncio.run(user_images.save_avatar(self.user_id, _b64(RED)))
        asyncio.run(user_images.save_provider_avatar(self.user_id, "trakt", BLUE))
        user_images.adopt_avatar_source(self.user_id, "trakt", only_if_missing=False)
        user_images.delete_avatar(self.user_id)

        user_images.delete_avatar_source(self.user_id, "trakt")
        self.assertFalse(user_images.has_avatar(self.user_id))

    def test_a_later_upload_replaces_what_you_fall_back_to(self):
        asyncio.run(user_images.save_avatar(self.user_id, _b64(RED)))
        asyncio.run(user_images.save_avatar(self.user_id, _b64(BLUE)))
        newest = user_images.avatar_path(self.user_id).read_bytes()
        asyncio.run(user_images.save_provider_avatar(self.user_id, "trakt", RED))
        user_images.adopt_avatar_source(self.user_id, "trakt", only_if_missing=False)

        user_images.delete_avatar_source(self.user_id, "trakt")
        self.assertEqual(user_images.avatar_path(self.user_id).read_bytes(), newest)


class ThePickerOffersEveryPictureTests(AppTestCase):
    """The account's own upload is a source like any other, so changing your mind
    about a service's picture is picking a different entry rather than deleting
    something."""

    def setUp(self):
        super().setUp()
        self.user_id = self.make_user("picker")
        user_images.delete_user_data(self.user_id)
        self.addCleanup(user_images.delete_user_data, self.user_id)
        self.sign_in_as(self.user_id)

    def test_the_upload_is_listed_beside_the_services_and_leads(self):
        asyncio.run(user_images.save_provider_avatar(self.user_id, "plex", BLUE))
        asyncio.run(user_images.save_avatar(self.user_id, _b64(RED)))
        self.assertEqual(user_images.list_avatar_sources(self.user_id),
                         ["uploaded", "plex"])

    def test_switching_back_to_your_own_needs_no_deletion(self):
        """THE POINT OF THE WHOLE CHANGE. Before this, the only route back to
        your own picture was removing the service's."""
        asyncio.run(user_images.save_avatar(self.user_id, _b64(RED)))
        mine = user_images.avatar_path(self.user_id).read_bytes()
        asyncio.run(user_images.save_provider_avatar(self.user_id, "plex", BLUE))

        self.client.post("/api/me/avatar/source", json={"source": "plex"})
        self.assertNotEqual(user_images.avatar_path(self.user_id).read_bytes(), mine)

        resp = self.client.post("/api/me/avatar/source", json={"source": "uploaded"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(user_images.avatar_path(self.user_id).read_bytes(), mine)
        # And the service's picture is still there to go back to.
        self.assertIn("plex", user_images.list_avatar_sources(self.user_id))

    def test_the_upload_cannot_be_refreshed(self):
        """Refresh means "ask them again" and the account's own upload has
        nobody to ask, so that route takes services only."""
        asyncio.run(user_images.save_avatar(self.user_id, _b64(RED)))
        resp = self.client.post("/api/me/avatar/source/refresh",
                                json={"source": "uploaded"})
        self.assertEqual(resp.status_code, 400)

    def test_a_linked_service_can_be_asked_for_its_picture_without_relinking(self):
        """An account linked before pictures were ever fetched has a connection
        and no picture. Pulling one must not require disconnecting first."""
        self.assertEqual(user_images.list_avatar_sources(self.user_id), [])
        with patch.object(provider_avatars, "current_url",
                          AsyncMock(return_value="https://media.trakt.tv/a.png")), \
             patch.object(provider_avatars, "fetch", AsyncMock(return_value=RED)):
            resp = self.client.post("/api/me/avatar/source/refresh",
                                    json={"source": "trakt"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(user_images.list_avatar_sources(self.user_id), ["trakt"])
