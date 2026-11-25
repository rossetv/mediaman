"""The Keep dialog holds, for each season it reads, the height the season has kept or not."""

from __future__ import annotations

import re
from pathlib import Path

import mediaman.web

_CSS = Path(mediaman.web.__file__).parent / "static" / "css"
_ROW = "#keep-season-list > .keep-season-item"


def _css() -> str:
    text = "".join((_CSS / name).read_text() for name in ("_modal.css", "_watch_list.css"))
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def _held(css: str) -> list[tuple[int, int]]:
    """The height and padding of a row where a season is kept: wide, by touch, narrowest."""
    rows = re.findall(
        re.escape("#keep-season-list.has-keeps > .keep-season-item")
        + r" \{ min-height: (\d+)px;(?: padding-block: (\d+)px;)? \}",
        css,
    )
    return [(int(height), int(pad or 6)) for height, pad in rows]


def test_a_kept_season_fits_the_row_held_for_it() -> None:
    wide, touch, narrow = _held(_css())
    # A 22px keep, 4px, and its button: 32px, by touch 44px.
    assert 22 + 4 + 32 + 2 * wide[1] <= wide[0]
    assert 22 + 4 + 44 + 2 * touch[1] <= touch[0]
    (rows,) = re.findall(r"--kept:not\([^)]*\) \{ grid-template-rows: (44px[^;]+);", _css())
    # The details are one line of 16px.
    lines = [int(r.removesuffix("px")) if r != "auto" else 16 for r in rows.split()]
    assert sum(lines) + 2 * narrow[1] <= narrow[0]


def test_rows_are_tall_only_where_a_season_is_kept() -> None:
    own = re.findall(re.escape(_ROW) + r" \{[^}]*?min-height: (\d+)px", _css())
    assert own and max(int(h) for h in own) < min(h for h, _ in _held(_css()))


def test_a_kept_season_never_wraps_to_more_lines_than_its_row_has() -> None:
    assert not re.search(r"\.keep-season-item--kept \{[^}]*flex-wrap", _css())
    assert not re.search(r"#keep-dialog-remove\.is-visible \{[^}]*100%", _css())


def _box(state: str) -> str:
    """What the sheet sets on a box to tick in this state, in the Keep dialog and Settings alike."""
    heads = [
        f'{row} input[type="checkbox"]{state}' for row in (".keep-season-item", ".recipient-item")
    ]
    return " ".join(re.findall(re.escape(",\n".join(heads)) + r" \{([^}]*)\}", _css()))


def test_a_box_to_tick_is_drawn_by_the_page_and_not_the_browser() -> None:
    assert "appearance: none" in _box("")
    assert "background: var(--accent-cta)" in _box(":checked")
    assert 'content: ""' in _box(":checked::after")


def test_the_system_draws_the_box_where_its_colours_are_forced() -> None:
    (forced,) = re.findall(r"@media \(forced-colors: active\) \{(.*?)\n\}", _css(), re.S)
    assert "appearance: auto" in forced and "content: none" in forced
