"""Text and type on a canvas: the primitives every renderer in this app shares.

WHAT THIS MODULE IS. Loading the bundled face at a size, measuring a string,
shortening it until it fits a box, drawing it inside that box, and masking an
image into a circle. Pixels and type, nothing else.

WHAT IT REFUSES TO BE. It has no feature vocabulary — no ranks, no tiers, no
months, no airings — and it never touches the database, the network, a request
object or a provider. A renderer decides WHAT to draw; this module only knows
HOW a string becomes ink. That separation is the whole point: it exists so that
two renderers measure a string the same way, and so that the eventual
font-fallback stack (for scripts the bundled Latin face has no glyphs for) is a
change to one function rather than a hunt through two renderers.

WHY A STRING IS ONLY EVER DRAWN INTO A BOX. Every string these renderers draw
comes from somewhere untrusted — a username, a provider's title. `draw_text`
takes a rectangle and ellipsizes to it, so the geometry is the bound and there
is no call shape that can run text off the edge of a canvas. That is why there
is no "draw at x, y" here and should not be one.

BLOCKING. Synchronous CPU work, like everything else that touches Pillow.
Callers on the event loop run their render in a worker thread.
"""
from __future__ import annotations

from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# The app's dark field and the text that sits on it. Both renderers start from
# these; a renderer that wants a different palette (a card tuned for the surface
# it lands on, say) declares its own colours in its own module rather than
# moving these, because these are what the in-app chrome is matched to.
BACKGROUND = (17, 19, 21)          # #111315
TEXT_COLOUR = (255, 255, 255)

_FONT_PATH = Path(__file__).resolve().parent / "static" / "fonts" / "Inter-Bold.ttf"

if not _FONT_PATH.exists():
    # Loud at import rather than quiet at render. The fallback Pillow offers,
    # `ImageFont.load_default()`, is an ~11px bitmap face — on any canvas this
    # app produces it is illegible, so a render that silently used it would look
    # broken to the user and fine to the server. The check lives beside the
    # loader, so every renderer that imports this gets it once.
    raise RuntimeError(
        f"Image rendering needs its bundled font at {_FONT_PATH}; it is missing."
    )


@lru_cache(maxsize=16)
def font(size: int) -> ImageFont.FreeTypeFont:
    """The bundled face at one size. Cached because a render asks for the same
    two or three sizes once per tile."""
    return ImageFont.truetype(str(_FONT_PATH), max(1, size))


def draw_text(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int],
    *,
    align: str = "center",
) -> None:
    """Draw `text` inside `box`, shortened with an ellipsis if it does not fit
    across. Always centred vertically; `align` decides the horizontal.

    THE ONLY PLACE THIS APP MEASURES OR DRAWS A STRING. Centring is done from
    `textbbox`, not from the string length: the bounding box accounts for the
    glyphs actually present, and it is what replaced the `textsize` that Pillow
    10 removed.

    `align="left"` exists for lines that sit directly beside something — an icon,
    a column edge — rather than floating in the space left over. It stays a
    parameter of this function rather than a second drawing path, because
    measurement and ellipsizing are the parts worth having in one place.
    """
    if not text:
        return
    left, top, right, bottom = box
    box_w = right - left
    box_h = bottom - top
    if box_w <= 0 or box_h <= 0:
        return

    shown = ellipsized(draw, text, font, box_w)
    if not shown:
        return
    x0, y0, x1, y1 = draw.textbbox((0, 0), shown, font=font)
    # Offset by the bbox origin as well as by the alignment: a glyph's ink does
    # not start at the pen position, and ignoring that leaves text visibly low in
    # its strip and a hair inside its left edge.
    x = left - x0 if align == "left" else left + (box_w - (x1 - x0)) / 2 - x0
    y = top + (box_h - (y1 - y0)) / 2 - y0
    draw.text((x, y), shown, font=font, fill=fill)


def ellipsized(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
               box_w: int) -> str:
    """`text`, trimmed until it fits `box_w`, with an ellipsis marking the cut."""
    def width(value: str) -> int:
        bbox = draw.textbbox((0, 0), value, font=font)
        return bbox[2] - bbox[0]

    if width(text) <= box_w:
        return text
    ellipsis = "…"
    trimmed = text
    while trimmed and width(trimmed + ellipsis) > box_w:
        trimmed = trimmed[:-1]
    return trimmed.rstrip() + ellipsis if trimmed else ""


def circular(raw: bytes, size: int) -> Image.Image | None:
    """`raw` as a circular RGBA tile, or None if the bytes turn out not to be an
    image. A bad icon costs the render its icon, never the render — an avatar is
    decoration, and no picture is a better outcome than no picture at all."""
    try:
        with Image.open(BytesIO(raw)) as source:
            icon = source.convert("RGBA").resize((size, size), Image.LANCZOS)
    except Exception:
        return None
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    icon.putalpha(mask)
    return icon
