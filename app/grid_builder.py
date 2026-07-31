"""Poster grids: pixels, and nothing else.

No database, no network, no request objects. The caller decides which titles are
in the image and in what order, resolves each one to a poster file on disk, and
hands the list here; this module turns that list into encoded bytes. It cannot
fetch a poster it was not given, which is what keeps rendering an offline
operation that never blocks on a provider.

GEOMETRY LIVES IN ONE FUNCTION. `compute_layout` answers where every tile,
number and caption goes, and both the renderer and the pre-render size check
call it. A second copy of that arithmetic would drift, and the way it would
announce itself is a refusal that disagrees with the image that was actually
produced — so there is exactly one.

TEXT GOES THROUGH ONE ENTRY POINT, AND IT IS NOT IN THIS MODULE.
`imaging.draw_text` is the only place a string here is measured or drawn: the
header line, the rank numbers and the captions all route through it. That
argument got STRONGER when the primitives moved out — an eventual font-fallback
stack (for scripts the bundled Latin face has no glyphs for) is now one change
that fixes every renderer in the app at once, rather than one that fixes this
one and leaves the next renderer drawing tofu.

BLOCKING. Everything here is synchronous CPU work and a full-size render takes
seconds. Callers on the event loop run `build_grid` in a worker thread, and
bound how many run at once — a 100-item canvas is tens of megapixels.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

from . import imaging

# ---------------------------------------------------------------------------
# constants — each one with the reason it has the value it has
# ---------------------------------------------------------------------------

# Exactly the size the poster cache normalizes to, which is also TMDB's w500
# rendition, so the common path pastes a tile with no resampling at all.
TILE_W, TILE_H = 500, 750

# The banner across the top: title, who made it, and an optional round icon.
# Both the icon and the type derive from this, so it is the one number that
# decides how legible the banner is. Sized against the 2500px-wide canvas a
# five-column grid comes to rather than against the tile: at half this, the name
# and the picture read as a caption on a poster wall instead of as its title.
HEADER_H = 140

# The strip under each tile that carries the rank number. It costs its height
# once per ROW, which makes it the constant that decides whether a tall grid
# still fits inside WebP's 16383px ceiling (see MAX_DIMENSION). 62 is as large as
# it can be while the biggest grid the caps allow — 100 titles in five columns,
# 16380px — still encodes as WebP. Raising it buys a bigger number at the cost of
# that combination, so raise it deliberately or not at all.
LABEL_H = 62

# Added under the rank strip only when captions are switched on, so a grid
# without them is not paying for the space.
CAPTION_H = 62

# Flush by default: the look this feature was designed around has no spacing at
# all. They are real constants rather than inlined zeros so that putting a gutter
# or a border back is a one-line change instead of a rewrite of the arithmetic.
GUTTER = 0
MARGIN = 0

# The podium row holds the top three, spanning the full content width, so they
# read as the result rather than as the first three of a list.
PODIUM_RANKS = 3

# The field and the text colour are `imaging.BACKGROUND` / `imaging.TEXT_COLOUR`
# — the app's dark chrome, shared with every other renderer. A second copy here
# would be a second thing to change when the chrome moves, and the drift would
# only show up as two exports that no longer match.
#
# The placeholder tile's own frame, a step up from the background so a missing
# poster looks deliberate rather than like a hole in the render.
PLACEHOLDER_BACKGROUND = (28, 31, 34)

# Type sizes as a fraction of the strip they sit in, so `scale` moves them with
# everything else rather than needing a second set of scaled constants.
_HEADER_TYPE = 0.60
_RANK_TYPE = 0.78
_CAPTION_TYPE = 0.58

# Per-format hard ceilings, measured rather than assumed. WebP's is the one that
# bites: a tall grid passes every other check and then throws from the encoder
# after the canvas has already been allocated and drawn.
MAX_DIMENSION = {"webp": 16383, "jpeg": 65535, "png": 2 ** 31 - 1}
FORMATS = frozenset(MAX_DIMENSION)

# Bumped whenever anything above changes where a pixel lands. A render cache
# keyed on the inputs alone would keep serving the old layout after the code
# that produced it was replaced.
RENDERER_VERSION = 3

_PLACEHOLDER_PATH = (
    Path(__file__).resolve().parent / "static" / "images" / "nopostertv.png"
)

# Loaded once. Converted here rather than per missing poster, because a grid can
# be a hundred tiles and most of them may have no artwork.
_PLACEHOLDER = Image.open(_PLACEHOLDER_PATH).convert("RGBA")
_PLACEHOLDER.load()


class GridError(Exception):
    """Something about the request cannot be rendered. The message is written to
    be shown to the person who asked for the export."""


class CanvasTooLarge(GridError):
    """The canvas this request implies is bigger than the chosen format can
    encode.

    Carries the numbers rather than only a sentence, so a caller can tell the
    user what they asked for, what the ceiling is, and which of the three knobs
    would get them under it.
    """

    def __init__(self, width: int, height: int, fmt: str, limit: int) -> None:
        self.width = width
        self.height = height
        self.fmt = fmt
        self.limit = limit
        super().__init__(
            f"That grid is {width}x{height} pixels, and {fmt.upper()} cannot encode "
            f"more than {limit} in either direction. Use more columns, half size, "
            f"or a different format."
        )


@dataclass(frozen=True)
class GridEntry:
    """One title's place in the image. `poster` is a file that already exists on
    disk, or None for the placeholder tile — this module never goes looking for
    one."""
    rank: int
    title: str = ""
    poster: Path | None = None
    # The tier this title came from, tinting its rank number. It is the one place
    # tier identity survives into the image.
    colour: str | None = None


@dataclass(frozen=True)
class Cell:
    """Where one entry's tile, number and caption go. Every rectangle is
    (left, top, right, bottom) in canvas pixels."""
    index: int
    tile: tuple[int, int, int, int]
    label: tuple[int, int, int, int]
    caption: tuple[int, int, int, int] | None


@dataclass(frozen=True)
class Layout:
    """The complete geometry of one grid. Produced by `compute_layout` and
    consumed by both the renderer and the format-limit check, so what is refused
    and what is drawn can never be computed differently."""
    count: int
    columns: int
    scale: float
    width: int
    height: int
    tile_w: int
    tile_h: int
    header_h: int
    label_h: int
    caption_h: int
    gutter: int
    margin: int
    rows: int
    podium: int
    cells: tuple[Cell, ...]

    def fits(self, fmt: str) -> bool:
        limit = MAX_DIMENSION[fmt]
        return self.width <= limit and self.height <= limit


def _scaled(value: int, scale: float) -> int:
    """A constant at the requested size. Never below 1: a zero-height strip
    would silently swallow the text it was meant to hold."""
    return max(1, round(value * scale))


def compute_layout(
    count: int,
    *,
    columns: int,
    scale: float = 1.0,
    show_titles: bool = False,
    podium: bool = False,
) -> Layout:
    """Where everything goes, for `count` entries in `columns` columns.

    Constants are scaled HERE, at layout time, rather than by shrinking a
    finished canvas — downscaling afterwards spends exactly the memory the
    smaller size was chosen to save, and softens every glyph on the way.

    The last row is centred when it is short. A 23-item 5-column grid otherwise
    ends on three tiles jammed left with a ragged gap beside them, which reads as
    a rendering fault rather than as the end of a list.
    """
    if columns < 1:
        raise GridError("A grid needs at least one column.")
    if count < 0:
        raise GridError("A grid cannot have a negative number of titles.")
    if scale <= 0:
        raise GridError("Scale must be greater than zero.")

    tile_w = _scaled(TILE_W, scale)
    tile_h = _scaled(TILE_H, scale)
    header_h = _scaled(HEADER_H, scale)
    label_h = _scaled(LABEL_H, scale)
    caption_h = _scaled(CAPTION_H, scale) if show_titles else 0
    gutter = round(GUTTER * scale)
    margin = round(MARGIN * scale)

    width = margin * 2 + columns * tile_w + (columns - 1) * gutter
    content_w = width - margin * 2

    cells: list[Cell] = []
    index = 0
    y = margin + header_h

    podium_count = min(PODIUM_RANKS, count) if podium else 0
    if podium_count:
        # Sized to span the content width rather than to a fixed multiple, so the
        # top three always sit on one full row whatever the column count is.
        # Integer division can leave a pixel or two over; the row is centred, so
        # what is left is split between its ends rather than hanging off one.
        podium_w = (content_w - (podium_count - 1) * gutter) // podium_count
        podium_h = round(podium_w * tile_h / tile_w)
        y = _place_row(cells, index, podium_count, podium_w, podium_h,
                       label_h, caption_h, gutter, margin, content_w, y)
        index += podium_count

    remaining = count - index
    rows = math.ceil(remaining / columns) if remaining else 0
    for row in range(rows):
        in_row = min(columns, remaining - row * columns)
        y = _place_row(cells, index, in_row, tile_w, tile_h,
                       label_h, caption_h, gutter, margin, content_w, y)
        index += in_row

    # `y` has a trailing gutter on it from the last row placed; the canvas ends
    # at the bottom of that row plus the margin.
    height = (y - gutter if cells else y) + margin
    return Layout(
        count=count, columns=columns, scale=scale, width=width, height=height,
        tile_w=tile_w, tile_h=tile_h, header_h=header_h, label_h=label_h,
        caption_h=caption_h, gutter=gutter, margin=margin, rows=rows,
        podium=podium_count, cells=tuple(cells),
    )


def _place_row(cells: list[Cell], first_index: int, in_row: int, tile_w: int,
               tile_h: int, label_h: int, caption_h: int, gutter: int,
               margin: int, content_w: int, y: int) -> int:
    """Append one row of cells and return the y the next row starts at."""
    row_w = in_row * tile_w + (in_row - 1) * gutter
    x = margin + (content_w - row_w) // 2
    for column in range(in_row):
        left = x + column * (tile_w + gutter)
        tile_bottom = y + tile_h
        label_bottom = tile_bottom + label_h
        cells.append(Cell(
            index=first_index + column,
            tile=(left, y, left + tile_w, tile_bottom),
            label=(left, tile_bottom, left + tile_w, label_bottom),
            caption=(left, label_bottom, left + tile_w, label_bottom + caption_h)
            if caption_h else None,
        ))
    return y + tile_h + label_h + caption_h + gutter


def ensure_fits(layout: Layout, fmt: str) -> None:
    """Raise CanvasTooLarge unless this layout can actually be encoded.

    Called BEFORE a canvas is allocated. Finding out from the encoder means
    finding out after the render, which for a hundred-item grid is half a minute
    of work and a few hundred megabytes spent to produce an exception.
    """
    fmt = _format(fmt)
    if not layout.fits(fmt):
        raise CanvasTooLarge(layout.width, layout.height, fmt, MAX_DIMENSION[fmt])


def _format(fmt: str) -> str:
    value = str(fmt or "").lower()
    if value not in FORMATS:
        raise GridError(f"Unknown image format {fmt!r}.")
    return value


def build_grid(
    entries: list[GridEntry],
    *,
    columns: int,
    title: str = "",
    username: str = "",
    header_image: bytes | None = None,
    scale: float = 1.0,
    fmt: str = "webp",
    show_titles: bool = False,
    podium: bool = False,
) -> bytes:
    """Render an ordered list of titles into an encoded image.

    `entries` are rendered exactly as given, in the order given — the slicing to
    a top-X and the decision about which tiers contribute happen before this is
    called. Returns the encoded bytes; their length is the file size the caller
    reports.
    """
    fmt = _format(fmt)
    if not entries:
        raise GridError("There is nothing to put in that grid.")

    layout = compute_layout(
        len(entries), columns=columns, scale=scale,
        show_titles=show_titles, podium=podium,
    )
    ensure_fits(layout, fmt)

    canvas = Image.new("RGB", (layout.width, layout.height), imaging.BACKGROUND)
    try:
        draw = ImageDraw.Draw(canvas)
        _draw_header(canvas, draw, layout, title=title, username=username,
                     header_image=header_image)

        # Padded to the width of the largest rank actually present, so 1..99
        # gives "01".."99" and a hundred-item grid gives "001".."100" with the
        # column still aligned. A fixed two digits breaks at exactly the maximum
        # this feature allows.
        digits = len(str(max(entry.rank for entry in entries)))
        rank_font = imaging.font(round(layout.label_h * _RANK_TYPE))
        caption_font = imaging.font(round(layout.caption_h * _CAPTION_TYPE)) \
            if layout.caption_h else None

        for cell in layout.cells:
            entry = entries[cell.index]
            _paste_tile(canvas, cell.tile, entry.poster)
            imaging.draw_text(
                draw, cell.label, str(entry.rank).zfill(digits), rank_font,
                _rank_colour(entry.colour),
            )
            if cell.caption is not None and caption_font is not None:
                imaging.draw_text(draw, cell.caption, entry.title, caption_font,
                                  imaging.TEXT_COLOUR)
        return _encode(canvas, fmt)
    finally:
        canvas.close()


def _rank_colour(colour: str | None) -> tuple[int, int, int]:
    """A tier's #RRGGBB, or white. An unreadable value falls back rather than
    failing an export over a colour."""
    if not colour or not colour.startswith("#") or len(colour) != 7:
        return imaging.TEXT_COLOUR
    try:
        return tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]
    except ValueError:
        return imaging.TEXT_COLOUR


def _draw_header(canvas: Image.Image, draw: ImageDraw.ImageDraw, layout: Layout, *,
                 title: str, username: str, header_image: bytes | None) -> None:
    """The banner: an optional round icon on the left, and the line naming whose
    list this is. With no icon the line simply centres across the full width."""
    left = layout.margin
    top = layout.margin
    right = layout.width - layout.margin
    bottom = top + layout.header_h

    # A gap of a third of the banner's height, used either as the inset from the
    # canvas edge or as the space between the icon and the line beside it.
    gap = max(1, layout.header_h // 3)
    text_left = left + gap
    if header_image:
        icon = imaging.circular(header_image, layout.header_h)
        if icon is not None:
            with icon:
                canvas.paste(icon, (left, top), icon)
            text_left = left + layout.header_h + gap

    # LEFT-ALIGNED, immediately beside the icon. Centred in the space left over,
    # the line drifts with the length of the name and stops reading as a label
    # belonging to the picture next to it.
    line = f"{username}'s {title}" if username and title else (title or username)
    imaging.draw_text(draw, (text_left, top, right, bottom), line,
                      imaging.font(round(layout.header_h * _HEADER_TYPE)),
                      imaging.TEXT_COLOUR, align="left")


def _paste_tile(canvas: Image.Image, box: tuple[int, int, int, int],
                poster: Path | None) -> None:
    """Fill one tile: the poster if there is a readable one, the placeholder
    otherwise.

    Each poster is opened, pasted and closed one at a time. Holding a list of
    open images would keep the whole grid's pixels resident at once, which for a
    hundred 500x750 tiles is the difference between a render that fits in memory
    and one that does not.
    """
    left, top, right, bottom = box
    width, height = right - left, bottom - top
    if poster is not None:
        try:
            with Image.open(poster) as source:
                # Lets the JPEG decoder skip straight to a smaller size when the
                # tile is smaller than the file, which is the half-scale case.
                source.draft("RGB", (width, height))
                tile = source.convert("RGB")
                if tile.size != (width, height):
                    tile = tile.resize((width, height), Image.LANCZOS)
                canvas.paste(tile, (left, top))
                tile.close()
            return
        except Exception:
            # An unreadable file is one tile's problem. The grid is still the
            # ranking the user asked for, with a placeholder where the artwork
            # should have been.
            pass
    _paste_placeholder(canvas, box)


@lru_cache(maxsize=8)
def _placeholder_tile(width: int, height: int) -> Image.Image:
    """The no-poster tile at one size, built once per size rather than per
    missing poster — a grid can be a hundred tiles and most of them may have no
    artwork.

    FILLED, NOT CENTRED. The source art is 321x431, which is not the 2:3 a
    poster is, so this does stretch it slightly. That was the choice made
    looking at real output: a smaller image floating in a larger tile reads as
    something that failed to load, while a filled tile reads as a deliberate
    stand-in and keeps the grid's rhythm.
    """
    tile = Image.new("RGB", (width, height), PLACEHOLDER_BACKGROUND)
    art = _PLACEHOLDER.resize((width, height), Image.LANCZOS)
    tile.paste(art, (0, 0), art)
    art.close()
    return tile


def _paste_placeholder(canvas: Image.Image, box: tuple[int, int, int, int]) -> None:
    """Fill one tile with the no-poster art."""
    left, top, right, bottom = box
    canvas.paste(_placeholder_tile(right - left, bottom - top), (left, top))


def _encode(canvas: Image.Image, fmt: str) -> bytes:
    """Encode the finished canvas.

    Qualities are the measured sweet spots: WebP q88 is the smallest of the
    three at this content, JPEG q90 trades some of that for opening anywhere,
    and PNG is for keeping. PNG's `optimize` buys single-digit percent for real
    CPU — worth taking on a format already chosen for archival, not worth
    expecting much from.
    """
    buffer = BytesIO()
    if fmt == "webp":
        canvas.save(buffer, format="WEBP", quality=88, method=4)
    elif fmt == "jpeg":
        canvas.save(buffer, format="JPEG", quality=90, subsampling=0)
    else:
        canvas.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
