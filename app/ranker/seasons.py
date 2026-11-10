"""What a season row of a board is called.

PURE: it is handed the words and answers with the label to print. No database,
no HTTP, no provider.

WHAT A ROW COUNTS is the title store's, read with the row
(app/metadata/labels.py's `board_columns`). WHAT A SHOW ROW'S SEASON CONTROL
OFFERS — every season a service lists, greyed where the board already holds it,
with one suggested — is the same rule every season picker in the app keeps, and
lives with them in app/providers/season_choices.py.
"""
from __future__ import annotations

# The one spelling of a season everywhere a picker draws one.
from ..providers.season_choices import season_word


def row_label(title: str | None, season: int | None, season_name: str | None = None) -> str:
    """WHAT A BOARD ROW IS CALLED — the row itself, its details heading and both
    exports, so the four cannot come to call one row two things.

    The whole show is its title. A season is "Show — Season N" ("Show — Specials"
    for 0), and a season with a name of its own is "Show — <name>" (True Detective
    — Night Country): an anthology's seasons are known by their names, not their
    numbers. The details panel's rule for what counts as a name is the service's
    (`SeasonsAnswer`'s `name`: a title that only restates the number is dropped
    there), so a stored name is used as it is.
    """
    part = season_part(season, season_name)
    return f"{title or 'Untitled'} — {part}" if part else (title or "Untitled")


def season_part(season: int | None, season_name: str | None = None) -> str:
    """The half of `row_label` after the dash: the season's name, else "Season
    N", and "" for the whole show. On its own because the board row draws the two
    halves apart — a long show name may be cut short there, and the season is the
    half that must survive it."""
    if season is None:
        return ""
    return season_name or season_word(season)
