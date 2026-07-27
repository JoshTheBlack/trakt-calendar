"""Per-user avatar and custom grid-header images: validate, normalize, store,
delete. Knows nothing about rankings, boards, or tiers.

STORAGE, so that account deletion is one rmtree:
    DATA_DIR/user_data/<user_id>/avatar.webp
    DATA_DIR/user_data/<user_id>/images/<image_uid>.webp

The avatar master is stored ONCE at 512x512 and derived sizes are produced on
request rather than cached — a resize of a single 512x512 image is cheap enough
that a second on-disk copy per size would only be a second source of truth to
keep in sync.

UPLOAD VALIDATION is the one thing worth pausing on, because it runs the same
way for both /me's own avatar and any image the ranker export flow lets a user
attach as a header icon:
  - the request-shape middleware only allows JSON bodies, so an upload arrives
    as base64 inside one; decoded bytes over MAX_UPLOAD_BYTES are refused
    before Pillow ever sees them.
  - `Image.open` runs inside a try — anything it can't parse is refused rather
    than raising out of the caller.
  - `img.format` is checked against an allowlist; a client-supplied filename or
    MIME type is never trusted, since either is just a string the client typed.
  - declared width/height are checked against MAX_SOURCE_DIMENSION before any
    pixel is decoded, and `Image.DecompressionBombError` is still caught
    explicitly in case a merely-tall-not-wide (or vice versa) image trips
    Pillow's own default pixel-count guard first.
  - the stored file is a NEW image with decoded pixels pasted in, never the
    original bytes re-encoded, so EXIF/ICC/comment metadata never survives —
    stripping by construction rather than by trying to enumerate what to strip.
"""
from __future__ import annotations

import base64
import binascii
import logging
import re
import shutil
import uuid
from io import BytesIO
from pathlib import Path

import anyio.to_thread
from PIL import Image

from .config import DATA_DIR

logger = logging.getLogger(__name__)

USER_DATA_DIR = DATA_DIR / "user_data"

MASTER_SIZE = 512
WEBP_QUALITY = 90

# Base64-decoded upload size. No legitimate avatar or header icon is anywhere
# near this large; it exists to bound how much a client can make Pillow chew on.
MAX_UPLOAD_BYTES = 4 * 1024 * 1024

# Checked against the image header before any pixel is decoded.
MAX_SOURCE_DIMENSION = 6000

ALLOWED_FORMATS = frozenset({"PNG", "JPEG", "WEBP", "GIF"})

MAX_IMAGES_PER_USER = 5

_UID_RE = re.compile(r"^[0-9a-f]{32}$")


class ValidationError(Exception):
    """An upload that failed one of the checks above. The message is safe to
    show the user directly — it never echoes anything from the file itself."""


class TooManyImages(Exception):
    """The account is already at MAX_IMAGES_PER_USER saved images."""


def _user_dir(user_id: int) -> Path:
    return USER_DATA_DIR / str(user_id)


def avatar_path(user_id: int) -> Path:
    return _user_dir(user_id) / "avatar.webp"


def images_dir(user_id: int) -> Path:
    return _user_dir(user_id) / "images"


def image_path(user_id: int, image_uid: str) -> Path:
    """Path for a saved custom image. `image_uid` is always server-generated
    (see add_image) except when it arrives back from a client in a DELETE URL,
    which is why callers that take one from a request validate its shape with
    `_valid_uid` first — a path segment built from an unvalidated string is
    exactly how `../../etc` becomes a path component."""
    return images_dir(user_id) / f"{image_uid}.webp"


def _valid_uid(image_uid: str) -> bool:
    return bool(image_uid) and bool(_UID_RE.fullmatch(image_uid))


def has_avatar(user_id: int) -> bool:
    return avatar_path(user_id).exists()


def list_images(user_id: int) -> list[str]:
    """Saved image uids for this account, oldest first."""
    d = images_dir(user_id)
    if not d.exists():
        return []
    entries: list[tuple[float, str]] = []
    for p in d.glob("*.webp"):
        try:
            entries.append((p.stat().st_mtime, p.stem))
        except OSError:
            continue
    entries.sort(key=lambda e: e[0])
    return [uid for _mtime, uid in entries]


def _decode_upload(image_b64: str) -> bytes:
    if not isinstance(image_b64, str) or not image_b64.strip():
        raise ValidationError("No image was provided.")
    payload = image_b64.strip()
    if payload.startswith("data:") and "," in payload:
        # A data: URL prefix, if the caller sent one whole. Only the payload
        # after the comma is base64.
        payload = payload.split(",", 1)[1]
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValidationError("That doesn't look like a valid image upload.") from exc
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValidationError(
            f"Images must be {MAX_UPLOAD_BYTES // (1024 * 1024)} MB or smaller.",
        )
    return raw


def _normalize(raw: bytes) -> bytes:
    """Decoded upload bytes -> a centre-cropped, 512x512 WebP master. Raises
    ValidationError on anything that doesn't hold up; run this off the event
    loop (see save_avatar / add_image)."""
    try:
        img = Image.open(BytesIO(raw))
    except Exception as exc:
        raise ValidationError("That file isn't a readable image.") from exc

    if img.width > MAX_SOURCE_DIMENSION or img.height > MAX_SOURCE_DIMENSION:
        raise ValidationError(
            f"Images larger than {MAX_SOURCE_DIMENSION}x{MAX_SOURCE_DIMENSION} aren't accepted.",
        )
    if img.format not in ALLOWED_FORMATS:
        raise ValidationError("Only PNG, JPEG, WebP, or GIF images are accepted.")

    try:
        img.load()
    except Image.DecompressionBombError as exc:
        raise ValidationError("That image is too large to process.") from exc
    except Exception as exc:
        raise ValidationError("That file isn't a readable image.") from exc

    # A NEW image, with only decoded pixels pasted in — the original bytes
    # (and any EXIF/ICC/comment metadata riding along with them) are never
    # touched again.
    clean = Image.new("RGBA", img.size)
    clean.paste(img.convert("RGBA"), (0, 0))

    side = min(clean.width, clean.height)
    left = (clean.width - side) // 2
    top = (clean.height - side) // 2
    square = clean.crop((left, top, left + side, top + side))
    square = square.resize((MASTER_SIZE, MASTER_SIZE), Image.LANCZOS)

    buf = BytesIO()
    square.save(buf, format="WEBP", quality=WEBP_QUALITY)
    return buf.getvalue()


async def _validated_master(image_b64: str) -> bytes:
    raw = _decode_upload(image_b64)
    return await anyio.to_thread.run_sync(_normalize, raw)


async def save_avatar(user_id: int, image_b64: str) -> Path:
    """Validate, normalize, and store the account's avatar, replacing whatever
    was there before."""
    normalized = await _validated_master(image_b64)
    path = avatar_path(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(normalized)
    return path


def delete_avatar(user_id: int) -> bool:
    try:
        avatar_path(user_id).unlink()
        return True
    except FileNotFoundError:
        return False


async def add_image(user_id: int, image_b64: str) -> str:
    """Validate, normalize, and store a new saved image. Returns its uid.
    Raises TooManyImages at the cap, checked before the (more expensive)
    validation so a client already at the limit isn't paying for a decode
    Pillow work it can't use."""
    if len(list_images(user_id)) >= MAX_IMAGES_PER_USER:
        raise TooManyImages()
    normalized = await _validated_master(image_b64)
    uid = uuid.uuid4().hex
    d = images_dir(user_id)
    d.mkdir(parents=True, exist_ok=True)
    image_path(user_id, uid).write_bytes(normalized)
    return uid


def delete_image(user_id: int, image_uid: str) -> bool:
    if not _valid_uid(image_uid):
        return False
    try:
        image_path(user_id, image_uid).unlink()
        return True
    except FileNotFoundError:
        return False


def resize_master(master: bytes, size: int) -> bytes:
    """A master WebP resized to `size`x`size`. Cheap enough to do per-request
    rather than caching derived sizes on disk — the whole point of a single
    512x512 master is that this is the only place a size ever gets produced."""
    img = Image.open(BytesIO(master)).convert("RGBA")
    if size != img.width:
        img = img.resize((size, size), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="WEBP", quality=WEBP_QUALITY)
    return buf.getvalue()


def delete_user_data(user_id: int) -> None:
    """Remove DATA_DIR/user_data/<user_id>/ entirely. Best-effort and logged on
    failure, exactly like every other cleanup sweep in this app — a failed
    delete here must not fail the account deletion it is cleaning up after.
    The shared poster cache is untouched: it holds no user-specific data."""
    d = _user_dir(user_id)
    try:
        shutil.rmtree(d)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("could not remove user data directory for user %s: %s", user_id, exc)
