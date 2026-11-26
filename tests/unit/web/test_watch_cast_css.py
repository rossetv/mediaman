"""The cast stylesheet: what a screen with no hover shows on a poster."""

from __future__ import annotations

import re
from pathlib import Path

CSS = (
    Path(__file__).resolve().parents[3] / "src/mediaman/web/static/css/_watch_cast.css"
).read_text()


def test_a_screen_without_hover_shows_a_posters_title_over_its_foot_and_keeps_its_height() -> None:
    block = re.search(r"@media \(hover: none\) \{(.*?)\n\}", CSS, re.S)
    assert block, "no hover: none block"
    rule = block.group(1)
    assert ".cast-card__poster-name" in rule
    assert "visibility: visible" in rule
    # Out of the grid's flow, so a title adds nothing to the row's height.
    assert "position: absolute" in rule


def test_with_less_motion_a_failed_picture_shows_its_letters_at_once() -> None:
    # The shared shortened transition would hold their visibility back for a frame.
    block = re.search(r"@media \(prefers-reduced-motion: reduce\) \{(.*?)\n\}", CSS, re.S)
    assert block, "no reduced-motion block"
    rule = re.search(
        r"\.cast-face__letters,\s*\.cast-card__poster-name \{([^}]*)\}", block.group(1)
    )
    assert rule and "transition: none !important" in rule.group(1)
