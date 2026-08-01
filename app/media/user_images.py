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
import json
import logging
import re
import shutil
import uuid
from io import BytesIO
from pathlib import Path

import anyio.to_thread
from PIL import Image

from ..config import DATA_DIR

logger = logging.getLogger(__name__)

USER_DATA_DIR = DATA_DIR / "user_data"

MASTER_SIZE = 512
WEBP_QUALITY = 90

# Base64-decoded upload size. No legitimate avatar or header icon is anywhere
# near this large; it exists to bound how much a client can make Pillow chew on.
MAX_UPLOAD_BYTES = 4 * 1024 * 1024

# The one thing to SAY about an image that is too big, wherever it is caught.
# The rule is stated in the units a person chose the file in, and it is the same
# sentence whether the size was noticed while reading the request or while
# decoding the payload — see MAX_UPLOAD_BODY_BYTES.
TOO_LARGE_MESSAGE = f"Images must be {MAX_UPLOAD_BYTES // (1024 * 1024)} MB or smaller."

# The largest REQUEST an image upload can legitimately be. The file travels
# base64-encoded inside a JSON object, which adds a third, and the wrapper around
# it (field names, quotes, an optional `data:` URL prefix, a name) adds a little
# more; the slack covers that with room to spare.
#
# WHY A ROUTE-LEVEL CAP AT ALL: authz.MAX_BODY_BYTES is the app-wide backstop and
# knows nothing about images, so it can only refuse in the request's terms —
# "that request is too large", which tells the person nothing they can act on.
# These two routes are the ones whose body has a KNOWN smaller ceiling, so they
# pass it, and the refusal reads as the image rule at every size instead of
# switching to the transport's wording once the file got big enough.
MAX_UPLOAD_BODY_BYTES = MAX_UPLOAD_BYTES * 4 // 3 + 64 * 1024

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


def generated_dir(user_id: int, year: int) -> Path:
    """Where images this account GENERATED are kept, grouped by year.

    Under the same per-account directory as its uploads, and for the same
    reason: deleting the account is one recursive remove, with nothing of theirs
    living anywhere else. Both segments are server-produced — an integer id and
    an integer year — so no user text ever becomes a path component.
    """
    return _user_dir(user_id) / "generated" / str(int(year))


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


# ---------------------------------------------------------------------------
# names
# ---------------------------------------------------------------------------
# A saved image needs a name, or a picker offering five of them is asking the
# user to recognize thirty-two hex characters.
#
# NAMES LIVE IN A SIDECAR FILE, NOT IN THE DATABASE, and the reason is that they
# belong to the file rather than to a row: the images are stored on disk, the
# account's directory is deleted wholesale when the account goes, and a table
# would have to be kept in step with a directory that can also be emptied by an
# operator with a broom. Keeping the label beside the thing it labels means the
# two cannot disagree — a name with no file is ignored on read, and a file with
# no name falls back to a numbered default.
#
# A name is never a path component (S13 still holds: paths are built only from
# the integer user id and the server-issued uid), so it is free text bounded
# only by a length.

MAX_IMAGE_NAME = 40

_NAMES_FILE = "images.json"


def _names_path(user_id: int) -> Path:
    return _user_dir(user_id) / _NAMES_FILE


def _read_names(user_id: int) -> dict[str, str]:
    """uid -> name. A missing or unreadable sidecar is an empty map rather than
    an error: a name is a convenience, and losing one must never cost somebody
    access to the image it was for."""
    try:
        raw = _names_path(user_id).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(k): str(v) for k, v in parsed.items() if isinstance(v, str)}


def _write_names(user_id: int, names: dict[str, str]) -> None:
    path = _names_path(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(names, sort_keys=True), encoding="utf-8")


def clean_name(value: object) -> str:
    """A user-supplied name, trimmed and bounded. Empty is allowed and means
    "no name": the caller substitutes a default rather than storing one, so an
    image the user never named does not look like one they named badly."""
    text = " ".join(str(value or "").split())
    if len(text) > MAX_IMAGE_NAME:
        raise ValidationError(f"A name can be at most {MAX_IMAGE_NAME} characters.")
    return text


def describe_images(user_id: int) -> list[dict[str, str]]:
    """Every saved image as {uid, name}, oldest first — what a picker needs to
    render something a person can choose between. An image with no stored name
    gets a positional default so there is always something to show."""
    names = _read_names(user_id)
    return [
        {"uid": uid, "name": names.get(uid) or f"Image {position}"}
        for position, uid in enumerate(list_images(user_id), start=1)
    ]


def set_image_name(user_id: int, image_uid: str, name: str) -> bool:
    """Name (or rename) a saved image. False when this account has no such
    image, so a caller answers 404 rather than storing a name for nothing."""
    if image_uid not in list_images(user_id):
        return False
    names = _read_names(user_id)
    if name:
        names[image_uid] = name
    else:
        names.pop(image_uid, None)
    _write_names(user_id, names)
    return True


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
        raise ValidationError(TOO_LARGE_MESSAGE)
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


async def add_image(user_id: int, image_b64: str, name: str = "") -> str:
    """Validate, normalize, and store a new saved image under an optional name.
    Returns its uid. Raises TooManyImages at the cap, checked before the (more
    expensive) validation so a client already at the limit isn't paying for a
    decode Pillow work it can't use."""
    if len(list_images(user_id)) >= MAX_IMAGES_PER_USER:
        raise TooManyImages()
    label = clean_name(name)
    normalized = await _validated_master(image_b64)
    uid = uuid.uuid4().hex
    d = images_dir(user_id)
    d.mkdir(parents=True, exist_ok=True)
    image_path(user_id, uid).write_bytes(normalized)
    if label:
        names = _read_names(user_id)
        names[uid] = label
        _write_names(user_id, names)
    return uid


def delete_image(user_id: int, image_uid: str) -> bool:
    if not _valid_uid(image_uid):
        return False
    try:
        image_path(user_id, image_uid).unlink()
    except FileNotFoundError:
        return False
    # The name goes with the image. Left behind it would be handed straight back
    # to the next upload that happened to reuse the uid — which cannot happen
    # with uuid4, but a stale map that only grows is its own small mess.
    names = _read_names(user_id)
    if names.pop(image_uid, None) is not None:
        _write_names(user_id, names)
    return True


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
