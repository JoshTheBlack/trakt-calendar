"""Provider picture slots: how they are written, adopted, refreshed and removed.

THE RULE THIS FILE EXISTS FOR: a slot may be overwritten freely — it is our copy
of somebody else's picture and a stale one is simply wrong — but `avatar.webp` is
set automatically ONLY when the account has none. After that it changes because
the person chose it, and never because they signed in again.
"""
from __future__ import annotations

import asyncio
import unittest
from io import BytesIO
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
        user_images.adopt_provider_avatar(self.user_id, "trakt", only_if_missing=True)
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
            user_images.adopt_provider_avatar(self.user_id, "plex", only_if_missing=True))
        self.assertEqual(user_images.avatar_path(self.user_id).read_bytes(), mine)

    def test_choosing_one_explicitly_does_replace_it(self):
        asyncio.run(user_images.save_avatar(self.user_id, _b64(RED)))
        asyncio.run(user_images.save_provider_avatar(self.user_id, "plex", BLUE))
        self.assertTrue(
            user_images.adopt_provider_avatar(self.user_id, "plex", only_if_missing=False))
        self.assertTrue(user_images.provider_avatar_is_adopted(self.user_id, "plex"))

    def test_removing_a_slot_takes_the_avatar_only_when_it_was_that_copy(self):
        asyncio.run(user_images.save_provider_avatar(self.user_id, "trakt", RED))
        asyncio.run(user_images.save_provider_avatar(self.user_id, "plex", BLUE))
        user_images.adopt_provider_avatar(self.user_id, "trakt", only_if_missing=True)

        # Removing the OTHER service's slot leaves the avatar alone.
        user_images.delete_provider_avatar(self.user_id, "plex")
        self.assertTrue(user_images.has_avatar(self.user_id))

        # Removing the one being worn takes it with it.
        user_images.delete_provider_avatar(self.user_id, "trakt")
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
        self.assertTrue(user_images.provider_avatar_is_adopted(self.user_id, "trakt"))

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
