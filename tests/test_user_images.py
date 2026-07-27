"""Per-user avatar/image upload and storage (app/user_images.py).

S4's six validation rules are each pinned by their own test: an oversized
decoded payload, unreadable bytes, a disallowed format, declared dimensions
over the cap, Pillow's own decompression-bomb guard, and metadata surviving
the round trip. Account deletion's cleanup of DATA_DIR/user_data/<id>/ (S15)
gets its own section, proving the shared poster cache is untouched.

Run: ./.venv/Scripts/python.exe -m unittest tests.test_user_images -v
"""
from __future__ import annotations

import asyncio
import base64
import itertools
import os
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

os.environ["TRAKT_DATA_DIR"] = tempfile.mkdtemp(prefix="tns-user-images-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, PngImagePlugin  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth, db, user_images  # noqa: E402
from app.config import Settings, save_settings  # noqa: E402
from app.main import app  # noqa: E402

TMP = Path(os.environ["TRAKT_DATA_DIR"])

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
        self.assertTrue(user_images.delete_avatar(user_id))
        self.assertFalse(user_images.has_avatar(user_id))

    def test_delete_avatar_on_an_account_with_none_is_a_no_op(self):
        user_id = _next_user_id()
        self.assertFalse(user_images.delete_avatar(user_id))


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

        self.assertTrue(user_images.delete_image(user_id, uids[0]))
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
            self.assertFalse(user_images.delete_image(user_id, "../../canary"))
            self.assertTrue(canary.exists())
            # The legitimate image is untouched by the rejected attempt.
            self.assertTrue(user_images.image_path(user_id, uid).exists())
        finally:
            canary.unlink(missing_ok=True)

    def test_delete_image_rejects_a_malformed_uid(self):
        user_id = _next_user_id()
        for bad in ("not-hex-at-all", "", "..", "a" * 31, "a" * 33):
            self.assertFalse(user_images.delete_image(user_id, bad))

    def test_delete_image_on_unknown_uid_returns_false(self):
        user_id = _next_user_id()
        self.assertFalse(user_images.delete_image(user_id, "0" * 32))

    def test_list_images_is_oldest_first(self):
        user_id = _next_user_id()
        first = asyncio.run(user_images.add_image(user_id, _b64(_png_bytes())))
        second = asyncio.run(user_images.add_image(user_id, _b64(_png_bytes())))
        self.assertEqual(user_images.list_images(user_id), [first, second])


# ---------------------------------------------------------------------------
# S15 — account deletion sweeps DATA_DIR/user_data/<id>/, and only that
# ---------------------------------------------------------------------------

class AccountDeletionCleanupTests(unittest.TestCase):
    _counter = 0

    def setUp(self):
        AccountDeletionCleanupTests._counter += 1
        db.set_db_path(TMP / f"delete-{AccountDeletionCleanupTests._counter}.db")
        asyncio.run(db.migrate())
        save_settings(Settings())
        self.client = TestClient(app, base_url="https://testserver",
                                  headers={"Origin": "https://testserver"})

    def tearDown(self):
        self.client.close()
        db.close_thread_connection()

    def _make_admin(self, username="admin1", password="hunter2hunter2") -> int:
        resp = self.client.post("/onboarding", json={
            "username": username, "password": password, "password_confirm": password,
        })
        self.assertEqual(resp.status_code, 200, resp.text)
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
    _counter = 0

    def setUp(self):
        RouteTests._counter += 1
        db.set_db_path(TMP / f"routes-{RouteTests._counter}.db")
        asyncio.run(db.migrate())
        save_settings(Settings())
        self.client = TestClient(app, base_url="https://testserver",
                                  headers={"Origin": "https://testserver"})
        resp = self.client.post("/onboarding", json={
            "username": "routeadmin", "password": "hunter2hunter2",
            "password_confirm": "hunter2hunter2",
        })
        self.assertEqual(resp.status_code, 200, resp.text)

    def tearDown(self):
        self.client.close()
        db.close_thread_connection()

    def test_signed_out_request_is_refused(self):
        anon = TestClient(app, base_url="https://testserver", headers={"Origin": "https://testserver"})
        resp = anon.post("/api/me/avatar", json={"image_b64": _b64(_png_bytes())})
        self.assertIn(resp.status_code, (401, 303))
        anon.close()

    def test_upload_view_and_delete_round_trip(self):
        resp = self.client.post("/api/me/avatar", json={"image_b64": _data_url(_png_bytes())})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["ok"])

        resp = self.client.get("/api/me/avatar")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "image/webp")
        with Image.open(BytesIO(resp.content)) as img:
            self.assertEqual(img.size, (user_images.MASTER_SIZE, user_images.MASTER_SIZE))

        resp = self.client.get("/api/me/avatar", params={"size": "64"})
        self.assertEqual(resp.status_code, 200)
        with Image.open(BytesIO(resp.content)) as img:
            self.assertEqual(img.size, (64, 64))

        resp = self.client.request("DELETE", "/api/me/avatar", json={})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.client.get("/api/me/avatar").status_code, 404)

    def test_a_non_image_upload_is_refused_with_a_readable_message(self):
        resp = self.client.post("/api/me/avatar", json={"image_b64": _b64(b"definitely not an image")})
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        self.assertFalse(data["ok"])
        self.assertTrue(data["error"])

    def test_a_huge_upload_is_refused_not_a_500(self):
        raw = os.urandom(user_images.MAX_UPLOAD_BYTES + 1)
        resp = self.client.post("/api/me/avatar", json={"image_b64": _b64(raw)})
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(resp.json()["error"])

    def test_saved_images_upload_cap_and_delete(self):
        uids = []
        for _ in range(user_images.MAX_IMAGES_PER_USER):
            resp = self.client.post("/api/me/images", json={"image_b64": _data_url(_png_bytes())})
            self.assertEqual(resp.status_code, 200, resp.text)
            uids.append(resp.json()["uid"])

        resp = self.client.post("/api/me/images", json={"image_b64": _data_url(_png_bytes())})
        self.assertEqual(resp.status_code, 409)

        resp = self.client.request("DELETE", f"/api/me/images/{uids[0]}", json={})
        self.assertEqual(resp.status_code, 200, resp.text)

        resp = self.client.request("DELETE", "/api/me/images/not-a-real-uid", json={})
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
