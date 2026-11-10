"""Per-user avatar/image upload and storage (app/media/user_images.py).

The six rules an upload has to satisfy are each pinned by their own test: an oversized
decoded payload, unreadable bytes, a disallowed format, declared dimensions
over the cap, Pillow's own decompression-bomb guard, and metadata surviving
the round trip. Account deletion's cleanup of DATA_DIR/user_data/<id>/
gets its own section, proving the shared poster cache is untouched.
"""
from __future__ import annotations

import asyncio
import base64
import itertools
import os
import re
import shutil
import unittest
from io import BytesIO
from unittest.mock import patch

from PIL import Image, PngImagePlugin
from fastapi.testclient import TestClient

from app import auth, db
from app.media import user_images
from app.config import Settings
from tests.support import save_settings, settling
from app.main import app
from tests.support import migrated_db

_user_id_seq = itertools.count(10_000)


def _next_user_id() -> int:
    return next(_user_id_seq)


def _png_bytes(size=(800, 600), color=(40, 60, 80)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _png_bytes_with_comment(size=(200, 200)) -> bytes:
    """A PNG carrying a tEXt chunk, so the metadata-stripping test has
    something concrete to prove is gone."""
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "not-anonymous")
    buf = BytesIO()
    Image.new("RGB", size, (10, 20, 30)).save(buf, format="PNG", pnginfo=info)
    return buf.getvalue()


def _bmp_bytes(size=(200, 200)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, (5, 5, 5)).save(buf, format="BMP")
    return buf.getvalue()


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _data_url(raw: bytes, mime="image/png") -> str:
    """Matches what FileReader.readAsDataURL sends from the browser — the
    server strips this prefix before decoding (see user_images._decode_upload)."""
    return f"data:{mime};base64,{_b64(raw)}"


# ---------------------------------------------------------------------------
# S4 — upload validation, one rule at a time
# ---------------------------------------------------------------------------

class UploadValidationTests(unittest.TestCase):
    def test_oversized_decoded_payload_rejected(self):
        raw = os.urandom(user_images.MAX_UPLOAD_BYTES + 1)
        with self.assertRaises(user_images.ValidationError):
            asyncio.run(user_images.save_avatar(_next_user_id(), _b64(raw)))

    def test_non_image_bytes_rejected(self):
        with self.assertRaises(user_images.ValidationError):
            asyncio.run(user_images.save_avatar(_next_user_id(), _b64(b"not an image, just text")))

    def test_malformed_base64_rejected(self):
        with self.assertRaises(user_images.ValidationError):
            asyncio.run(user_images.save_avatar(_next_user_id(), "%%%not-base64%%%"))

    def test_missing_payload_rejected(self):
        with self.assertRaises(user_images.ValidationError):
            asyncio.run(user_images.save_avatar(_next_user_id(), ""))

    def test_disallowed_format_rejected(self):
        with self.assertRaises(user_images.ValidationError):
            asyncio.run(user_images.save_avatar(_next_user_id(), _b64(_bmp_bytes())))

    def test_declared_dimensions_over_cap_rejected(self):
        # A thin strip keeps this cheap to decode while still declaring a width
        # past MAX_SOURCE_DIMENSION, which is checked before Pillow ever loads
        # pixel data.
        oversized = _png_bytes(size=(user_images.MAX_SOURCE_DIMENSION + 1, 10))
        with self.assertRaises(user_images.ValidationError):
            asyncio.run(user_images.save_avatar(_next_user_id(), _b64(oversized)))

    def test_decompression_bomb_caught(self):
        # Pillow's own guard fires against MAX_IMAGE_PIXELS, independent of our
        # dimension cap; lowering it here makes an ordinary small image trip it
        # so the except branch is exercised directly rather than hypothetically.
        with patch.object(Image, "MAX_IMAGE_PIXELS", 100):
            with self.assertRaises(user_images.ValidationError):
                asyncio.run(user_images.save_avatar(_next_user_id(), _b64(_png_bytes(size=(200, 200)))))

    def test_metadata_is_stripped(self):
        user_id = _next_user_id()
        asyncio.run(user_images.save_avatar(user_id, _b64(_png_bytes_with_comment())))
        with Image.open(user_images.avatar_path(user_id)) as stored:
            stored.load()
            self.assertNotIn("comment", {k.lower() for k in stored.info})

    def test_data_url_prefix_is_accepted(self):
        user_id = _next_user_id()
        asyncio.run(user_images.save_avatar(user_id, _data_url(_png_bytes())))
        self.assertTrue(user_images.has_avatar(user_id))


# ---------------------------------------------------------------------------
# the 512x512 master
# ---------------------------------------------------------------------------

class MasterSizeTests(unittest.TestCase):
    def test_master_is_512x512_webp(self):
        user_id = _next_user_id()
        asyncio.run(user_images.save_avatar(user_id, _b64(_png_bytes(size=(800, 600)))))
        with Image.open(user_images.avatar_path(user_id)) as img:
            self.assertEqual(img.format, "WEBP")
            self.assertEqual(img.size, (user_images.MASTER_SIZE, user_images.MASTER_SIZE))

    def test_non_square_source_is_center_cropped_not_stretched(self):
        # A wide source cropped to a square shouldn't retain its original
        # aspect ratio anywhere in the pipeline; the only assertion available
        # from the outside is that the master always comes out square.
        user_id = _next_user_id()
        asyncio.run(user_images.save_avatar(user_id, _b64(_png_bytes(size=(1200, 300)))))
        with Image.open(user_images.avatar_path(user_id)) as img:
            self.assertEqual(img.size, (user_images.MASTER_SIZE, user_images.MASTER_SIZE))

    def test_resize_master_produces_requested_size(self):
        user_id = _next_user_id()
        asyncio.run(user_images.save_avatar(user_id, _b64(_png_bytes())))
        master = user_images.avatar_path(user_id).read_bytes()
        small = user_images.resize_master(master, 70)
        with Image.open(BytesIO(small)) as img:
            self.assertEqual(img.size, (70, 70))


# ---------------------------------------------------------------------------
# avatar storage: replace, delete, reflect state
# ---------------------------------------------------------------------------

class AvatarStorageTests(unittest.TestCase):
    def test_has_avatar_false_until_uploaded(self):
        user_id = _next_user_id()
        self.assertFalse(user_images.has_avatar(user_id))
        asyncio.run(user_images.save_avatar(user_id, _b64(_png_bytes())))
        self.assertTrue(user_images.has_avatar(user_id))

    def test_reupload_replaces_rather_than_accumulates(self):
        user_id = _next_user_id()
        asyncio.run(user_images.save_avatar(user_id, _b64(_png_bytes(size=(300, 300), color=(200, 30, 30)))))
        first = user_images.avatar_path(user_id).read_bytes()
        asyncio.run(user_images.save_avatar(user_id, _b64(_png_bytes(size=(900, 400), color=(30, 30, 200)))))
        second = user_images.avatar_path(user_id).read_bytes()
        self.assertNotEqual(first, second)
        # Exactly one file for this user's avatar, not one per upload.
        self.assertEqual(list(user_images._user_dir(user_id).glob("avatar.*")), [user_images.avatar_path(user_id)])

    def test_delete_avatar_removes_file_and_reports_it(self):
        user_id = _next_user_id()
        asyncio.run(user_images.save_avatar(user_id, _b64(_png_bytes())))
        self.assertTrue(asyncio.run(user_images.delete_avatar(user_id)))
        self.assertFalse(user_images.has_avatar(user_id))

    def test_delete_avatar_on_an_account_with_none_is_a_no_op(self):
        user_id = _next_user_id()
        self.assertFalse(asyncio.run(user_images.delete_avatar(user_id)))


# ---------------------------------------------------------------------------
# saved images: cap, delete, and the path-safety of a client-supplied uid
# ---------------------------------------------------------------------------

class SavedImageTests(unittest.TestCase):
    def test_add_image_returns_a_uid_and_stores_a_file(self):
        user_id = _next_user_id()
        uid = asyncio.run(user_images.add_image(user_id, _b64(_png_bytes())))
        self.assertIn(uid, user_images.list_images(user_id))
        self.assertTrue(user_images.image_path(user_id, uid).exists())

    def test_cap_is_enforced_and_delete_frees_a_slot(self):
        user_id = _next_user_id()
        uids = [asyncio.run(user_images.add_image(user_id, _b64(_png_bytes())))
                for _ in range(user_images.MAX_IMAGES_PER_USER)]
        with self.assertRaises(user_images.TooManyImages):
            asyncio.run(user_images.add_image(user_id, _b64(_png_bytes())))

        self.assertTrue(asyncio.run(user_images.delete_image(user_id, uids[0])))
        # A slot freed by the delete is usable again.
        asyncio.run(user_images.add_image(user_id, _b64(_png_bytes())))
        self.assertEqual(len(user_images.list_images(user_id)), user_images.MAX_IMAGES_PER_USER)

    def test_delete_image_rejects_a_path_traversal_uid(self):
        user_id = _next_user_id()
        uid = asyncio.run(user_images.add_image(user_id, _b64(_png_bytes())))
        # A canary file outside this user's images/ directory that a traversal
        # attempt through the uid would target.
        canary = user_images._user_dir(user_id).parent / "canary.webp"
        canary.write_bytes(b"do-not-touch")
        try:
            self.assertFalse(asyncio.run(user_images.delete_image(user_id, "../../canary")))
            self.assertTrue(canary.exists())
            # The legitimate image is untouched by the rejected attempt.
            self.assertTrue(user_images.image_path(user_id, uid).exists())
        finally:
            canary.unlink(missing_ok=True)

    def test_delete_image_rejects_a_malformed_uid(self):
        user_id = _next_user_id()
        for bad in ("not-hex-at-all", "", "..", "a" * 31, "a" * 33):
            self.assertFalse(asyncio.run(user_images.delete_image(user_id, bad)))

    def test_delete_image_on_unknown_uid_returns_false(self):
        user_id = _next_user_id()
        self.assertFalse(asyncio.run(user_images.delete_image(user_id, "0" * 32)))

    def test_list_images_is_oldest_first(self):
        user_id = _next_user_id()
        first = asyncio.run(user_images.add_image(user_id, _b64(_png_bytes())))
        second = asyncio.run(user_images.add_image(user_id, _b64(_png_bytes())))
        self.assertEqual(user_images.list_images(user_id), [first, second])


class ImageNameTests(unittest.TestCase):
    """Names, which live in a sidecar file beside the images rather than in the
    database — so a directory removed by hand takes its names with it and cannot
    leave rows describing files that are gone."""

    def test_an_image_uploaded_with_a_name_reports_it(self):
        user_id = _next_user_id()
        uid = asyncio.run(user_images.add_image(user_id, _b64(_png_bytes()), "Beach photo"))
        self.assertEqual(asyncio.run(user_images.describe_images(user_id)),
                         [{"uid": uid, "name": "Beach photo"}])

    def test_an_unnamed_image_gets_a_positional_default(self):
        """There is always something to show in a picker: an image nobody named
        is "Image 1", not an empty row."""
        user_id = _next_user_id()
        first = asyncio.run(user_images.add_image(user_id, _b64(_png_bytes())))
        second = asyncio.run(user_images.add_image(user_id, _b64(_png_bytes())))
        self.assertEqual(asyncio.run(user_images.describe_images(user_id)),
                         [{"uid": first, "name": "Image 1"},
                          {"uid": second, "name": "Image 2"}])

    def test_renaming_and_clearing_a_name(self):
        user_id = _next_user_id()
        uid = asyncio.run(user_images.add_image(user_id, _b64(_png_bytes()), "First"))
        self.assertTrue(asyncio.run(user_images.set_image_name(user_id, uid, "Second")))
        self.assertEqual(asyncio.run(user_images.describe_images(user_id))[0]["name"], "Second")
        # Cleared falls back to the default rather than being stored as blank.
        self.assertTrue(asyncio.run(user_images.set_image_name(user_id, uid, "")))
        self.assertEqual(asyncio.run(user_images.describe_images(user_id))[0]["name"], "Image 1")

    def test_naming_an_image_this_account_does_not_have_is_refused(self):
        user_id = _next_user_id()
        self.assertFalse(asyncio.run(user_images.set_image_name(user_id, "0" * 32, "Nice try")))

    def test_an_over_long_name_is_refused_not_truncated(self):
        user_id = _next_user_id()
        with self.assertRaises(user_images.ValidationError):
            asyncio.run(user_images.add_image(
                user_id, _b64(_png_bytes()), "x" * (user_images.MAX_IMAGE_NAME + 1)))
        # And nothing was stored for the upload that was refused.
        self.assertEqual(user_images.list_images(user_id), [])

    def test_a_deleted_image_takes_its_name_with_it(self):
        user_id = _next_user_id()
        uid = asyncio.run(user_images.add_image(user_id, _b64(_png_bytes()), "Gone soon"))
        self.assertTrue(asyncio.run(user_images.delete_image(user_id, uid)))
        self.assertEqual(asyncio.run(user_images.describe_images(user_id)), [])
        self.assertEqual(user_images._read_names(user_id), {})

    def test_an_unreadable_sidecar_costs_the_names_and_nothing_else(self):
        """A name is a convenience. Losing the file that holds them must never
        cost somebody access to the images themselves."""
        user_id = _next_user_id()
        uid = asyncio.run(user_images.add_image(user_id, _b64(_png_bytes()), "Named"))
        user_images._names_path(user_id).write_text("{not json", encoding="utf-8")
        self.assertEqual(asyncio.run(user_images.describe_images(user_id)),
                         [{"uid": uid, "name": "Image 1"}])


# ---------------------------------------------------------------------------
# Account deletion sweeps DATA_DIR/user_data/<id>/, and only that
# ---------------------------------------------------------------------------

class AccountDeletionCleanupTests(unittest.TestCase):
    def setUp(self):
        migrated_db("delete")
        save_settings(Settings())
        self.client = TestClient(settling(app), base_url="https://testserver",
                                  headers={"Origin": "https://testserver"})

    def tearDown(self):
        self.client.close()
        db.close_thread_connection()

    def _make_admin(self, username="admin1", password="hunter2hunter2") -> int:
        resp = self.client.post("/onboarding", json={
            "username": username, "password": password, "password_confirm": password,
        })
        self.assertEqual(resp.status_code, 204, resp.text)
        return asyncio.run(auth.find_user_by_username(username))["id"]

    def test_deleting_account_removes_the_user_data_directory(self):
        admin_id = self._make_admin()
        victim_id = asyncio.run(auth.create_user(username="victim", password="memberpass1"))
        asyncio.run(user_images.save_avatar(victim_id, _b64(_png_bytes())))
        asyncio.run(user_images.add_image(victim_id, _b64(_png_bytes())))
        user_dir = user_images._user_dir(victim_id)
        self.assertTrue(user_dir.exists())

        asyncio.run(auth.delete_user(victim_id, actor_user_id=admin_id))

        self.assertFalse(user_dir.exists())

    def test_deleting_an_account_with_no_uploads_does_not_raise(self):
        admin_id = self._make_admin()
        victim_id = asyncio.run(auth.create_user(username="victim2", password="memberpass1"))
        self.assertFalse(user_images._user_dir(victim_id).exists())
        # Must not raise even though there is nothing on disk to remove.
        asyncio.run(auth.delete_user(victim_id, actor_user_id=admin_id))

    def test_shared_poster_cache_is_untouched_by_account_deletion(self):
        admin_id = self._make_admin()
        victim_id = asyncio.run(auth.create_user(username="victim3", password="memberpass1"))
        asyncio.run(user_images.save_avatar(victim_id, _b64(_png_bytes())))

        poster_dir = user_images.DATA_DIR / "posters" / "show"
        poster_dir.mkdir(parents=True, exist_ok=True)
        poster_file = poster_dir / "1396.jpg"
        poster_file.write_bytes(b"shared, not user data")

        asyncio.run(auth.delete_user(victim_id, actor_user_id=admin_id))

        self.assertTrue(poster_file.exists())


# ---------------------------------------------------------------------------
# the routes themselves — JSON in, base64 payload, gating
# ---------------------------------------------------------------------------

class RouteTests(unittest.TestCase):
    def setUp(self):
        migrated_db("routes")
        # A fresh database has to mean a fresh disk too: onboarding hands out the
        # same user id in every test, so images left by an earlier one would be
        # sitting exactly where this one's account looks for its own.
        shutil.rmtree(user_images.USER_DATA_DIR, ignore_errors=True)
        save_settings(Settings())
        self.client = TestClient(settling(app), base_url="https://testserver",
                                  headers={"Origin": "https://testserver"})
        resp = self.client.post("/onboarding", json={
            "username": "routeadmin", "password": "hunter2hunter2",
            "password_confirm": "hunter2hunter2",
        })
        self.assertEqual(resp.status_code, 204, resp.text)

    def tearDown(self):
        self.client.close()
        db.close_thread_connection()

    def test_signed_out_request_is_refused(self):
        anon = TestClient(settling(app), base_url="https://testserver", headers={"Origin": "https://testserver"})
        resp = anon.post("/me/avatar", json={"image_b64": _b64(_png_bytes())})
        self.assertIn(resp.status_code, (401, 303))
        anon.close()

    def avatar_error(self, resp) -> str:
        """The sentence beside the Upload button, which is where a refusal is drawn."""
        self.assertEqual(resp.status_code, 200, resp.text)
        return re.search(r'id="avatarError" role="alert">(.*?)</span>', resp.text, re.S).group(1)

    def test_upload_view_and_delete_round_trip(self):
        resp = self.client.post("/me/avatar", json={"image_b64": _data_url(_png_bytes())})
        self.assertEqual(self.avatar_error(resp), "")
        # The section is drawn with the picture and the confirmation that removes it.
        self.assertIn("/api/me/avatar?size=96&amp;v=", resp.text)
        self.assertIn('id="confirm-avatar-remove"', resp.text)

        resp = self.client.get("/api/me/avatar")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "image/webp")
        with Image.open(BytesIO(resp.content)) as img:
            self.assertEqual(img.size, (user_images.MASTER_SIZE, user_images.MASTER_SIZE))

        resp = self.client.get("/api/me/avatar", params={"size": "64"})
        self.assertEqual(resp.status_code, 200)
        with Image.open(BytesIO(resp.content)) as img:
            self.assertEqual(img.size, (64, 64))

        resp = self.client.post("/me/avatar/remove", json={})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("avatar-placeholder", resp.text)
        self.assertNotIn('id="confirm-avatar-remove"', resp.text)
        self.assertEqual(self.client.get("/api/me/avatar").status_code, 404)

    def test_a_non_image_upload_is_refused_with_a_readable_message(self):
        resp = self.client.post("/me/avatar", json={"image_b64": _b64(b"definitely not an image")})
        self.assertTrue(self.avatar_error(resp))
        self.assertFalse(user_images.has_avatar(
            asyncio.run(auth.find_user_by_username("routeadmin"))["id"]))

    def test_a_huge_upload_is_refused_not_a_500(self):
        raw = os.urandom(user_images.MAX_UPLOAD_BYTES + 1)
        resp = self.client.post("/me/avatar", json={"image_b64": _b64(raw)})
        self.assertTrue(self.avatar_error(resp))

    def test_the_upload_is_the_shared_file_reader_and_posts_when_it_has_read(self):
        """No near-copy of the reader: the input names ui.js's `data-file-into`,
        and the upload form posts on the event that reader fires once the field is
        filled."""
        page = self.client.get("/me").text
        section = page.split('id="accountAvatar"', 1)[1].split('id="imageLibrary"', 1)[0]
        self.assertRegex(section, r'<input type="file"[^>]*data-file-into="#avatarData"[^>]*data-file-as="base64"')
        self.assertRegex(section, r'<form hidden[^>]*hx-post="/me/avatar" hx-json-enc hx-trigger="file-read"')
        self.assertIn('name="image_b64" id="avatarData"', section)
        from app import assets
        reader = (assets.BASE_DIR / "static/js/ui.js").read_text(encoding="utf-8")
        self.assertIn("new Event('file-read', { bubbles: true })", reader)

    # THE SAVED-PICTURE LIBRARY IS MARKUP, so these ask what it drew rather than
    # what a JSON list held. What each action answers with is the whole list —
    # a cleared name falls back to a position, and an upload and a delete both
    # change how many are left, so the list is the unit that is always right.

    def uid_in(self, html: str) -> str:
        return re.search(r"/api/me/images/([0-9a-f]{32})", html).group(1)

    def test_saved_images_upload_cap_and_delete(self):
        uids = []
        for _ in range(user_images.MAX_IMAGES_PER_USER):
            resp = self.client.post("/me/images", json={"image_b64": _data_url(_png_bytes())})
            self.assertEqual(resp.status_code, 200, resp.text)
            uids = re.findall(r"/api/me/images/([0-9a-f]{32})", resp.text)
        self.assertEqual(len(uids), user_images.MAX_IMAGES_PER_USER)

        full = self.client.post("/me/images", json={"image_b64": _data_url(_png_bytes())})
        self.assertIn(f"up to {user_images.MAX_IMAGES_PER_USER} images", full.text)
        # And the upload control is gone from a library that is full.
        self.assertNotIn('id="imageFile"', full.text)

        gone = self.client.post(f"/me/images/{uids[0]}/delete", json={})
        self.assertNotIn(uids[0], gone.text)
        self.assertIn('id="imageFile"', gone.text)

        missing = self.client.post("/me/images/not-a-real-uid/delete", json={})
        self.assertIn("No such image.", missing.text)

    def test_naming_an_image_over_http(self):
        created = self.client.post(
            "/me/images", json={"image_b64": _data_url(_png_bytes()), "name": "Holiday"})
        uid = self.uid_in(created.text)
        self.assertIn('value="Holiday"', created.text)
        self.assertIn(f'maxlength="{user_images.MAX_IMAGE_NAME}"', created.text)

        renamed = self.client.post(f"/me/images/{uid}/name", json={"name": "Holiday 2019"})
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertIn('value="Holiday 2019"', renamed.text)

    def test_renaming_an_image_that_is_not_this_accounts_says_so_and_changes_nothing(self):
        created = self.client.post("/me/images", json={"image_b64": _data_url(_png_bytes())})
        uid = self.uid_in(created.text)
        stranger = asyncio.run(auth.create_user(
            username="stranger", password="hunter2hunter2", settings=Settings()))
        self.client.cookies.set(auth.COOKIE_NAME_SECURE,
                                asyncio.run(auth.create_session(stranger)))
        resp = self.client.post(f"/me/images/{uid}/name", json={"name": "Mine now"})
        self.assertIn("No such image.", resp.text)
        self.assertNotIn(uid, resp.text)

    def test_an_over_long_name_is_refused_over_http(self):
        created = self.client.post("/me/images", json={"image_b64": _data_url(_png_bytes())})
        uid = self.uid_in(created.text)
        resp = self.client.post(f"/me/images/{uid}/name",
                                json={"name": "x" * (user_images.MAX_IMAGE_NAME + 1)})
        self.assertIn(f"at most {user_images.MAX_IMAGE_NAME}", resp.text)
        self.assertIn('value="Image 1"', resp.text)


class AFileIsReadAndWrittenOffTheLoopTests(unittest.TestCase):
    """Every coroutine here reads and writes its files on a worker thread: its
    caller is a request on the event loop, and a read or a write there waits a
    switch interval for the interpreter lock at each system call while another
    thread is busy (the module docstring)."""

    def _threads_touching_files(self, work) -> set[int]:
        import pathlib
        import threading
        touched: set[int] = set()

        def recording(real):
            def method(self, *args, **kwargs):
                touched.add(threading.get_ident())
                return real(self, *args, **kwargs)
            return method

        with patch.multiple(pathlib.Path, **{
                name: recording(getattr(pathlib.Path, name))
                for name in ("read_bytes", "write_bytes", "read_text", "write_text", "unlink")}):
            asyncio.run(work())
        return touched

    def test_every_picture_coroutine_touches_its_files_off_the_calling_thread(self):
        import threading
        user_id = _next_user_id()
        png = _png_bytes(size=(64, 64))

        async def upload():
            await user_images.save_avatar(user_id, _b64(png))

        async def service():
            await user_images.save_provider_avatar(user_id, "trakt", png)
            await user_images.adopt_avatar_source(user_id, "trakt", only_if_missing=False)
            await user_images.avatar_source_is_adopted(user_id, "trakt")
            await user_images.account_pictures(user_id)

        async def library():
            uid = await user_images.add_image(user_id, _b64(png), "Named")
            await user_images.set_image_name(user_id, uid, "Renamed")
            await user_images.describe_images(user_id)
            await user_images.delete_image(user_id, uid)

        async def resize():
            await user_images.resized(user_images.avatar_path(user_id), 32)

        async def removal():
            await user_images.delete_avatar_source(user_id, "trakt")
            await user_images.delete_avatar(user_id)

        for work in (upload, service, library, resize, removal):
            with self.subTest(work=work.__name__):
                touched = self._threads_touching_files(work)
                self.assertTrue(touched, "the work touched no file at all")
                self.assertNotIn(threading.get_ident(), touched)

    def test_a_resize_of_a_picture_that_has_gone_is_none(self):
        self.assertIsNone(asyncio.run(user_images.resized(
            user_images.avatar_path(_next_user_id()), 32)))


if __name__ == "__main__":
    unittest.main()
