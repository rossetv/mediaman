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


def test_a_kept_season_fits_the_row_held_for_it_on_a_phone() -> None:
    css = _css()
    # (the row's height, its padding) at each width the kept row lays itself out for
    held = re.findall(re.escape(_ROW) + r" \{[^}]*?min-height: (\d+)px;([^}]*)\}", css)
    kept = re.findall(
        re.escape(_ROW) + r"--kept:not\(\.keep-season-item--skel\) "
        r"\{ grid-template-rows: ([^;]+); row-gap: (\d+)px; \}",
        css,
    )
    assert len(held) == len(kept) + 1 == 3  # wide rows have one line of controls
    padding = 6
    for (height, rest), (rows, gap) in zip(held[1:], kept, strict=True):
        own = re.search(r"padding-block: (\d+)px", rest)
        padding = int(own[1]) if own else padding
        # The last line is the details: 16px.
        lines = [int(r.removesuffix("px")) if r != "auto" else 16 for r in rows.split()]
        assert sum(lines) + int(gap) * (len(lines) - 1) + 2 * padding <= int(height)


def test_a_kept_season_never_wraps_to_more_lines_than_its_row_has() -> None:
    assert not re.search(r"\.keep-season-item--kept \{[^}]*flex-wrap", _css())
    assert not re.search(r"#keep-dialog-remove\.is-visible \{[^}]*100%", _css())
