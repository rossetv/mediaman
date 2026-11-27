"""The mailed page's steps: labels keep a gap and a phone's row holds two lines."""

from __future__ import annotations

import re

from tests.unit.web._css import px, style

SHEET = "_download_page.css"


def test_a_step_label_keeps_a_gap_before_the_next_step() -> None:
    assert px(style(".share-steps li:not(:last-child)", SHEET)["padding-right"]) > 0


def test_a_phones_step_row_holds_two_lines_so_a_longer_label_does_not_move_the_page() -> None:
    held = style(".share-steps span", SHEET, "(max-width: 699.98px)")["min-height"]
    lines = re.fullmatch(r"([\d.]+)lh", held)
    assert lines and float(lines[1]) >= 2, held
