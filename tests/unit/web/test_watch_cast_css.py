"""The cast stylesheet: what a screen with no hover shows on a poster, and how wide a person's cell is."""

from __future__ import annotations

import re
from pathlib import Path

from tests.unit.web._css import medias, px, rules, style

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


# "Featherstonehaugh" in the name's type: Chromium does not hyphenate a capitalised word, so a
# surname wider than its cell is cut mid-word with no hyphen.
LONG_SURNAME_PX = 109


def test_a_cast_cell_holds_a_long_surname_whole_at_every_width() -> None:
    wide = style(".ttl-cast", "_watch_cast.css")["grid-template-columns"]
    least = re.search(r"minmax\((\d+)px", wide)
    assert least and int(least.group(1)) >= LONG_SURNAME_PX
    # On a phone four columns are fixed; they hold the surname only from some 520px of window
    # (cells of 82px at 402px wide cut it), so below that there are three.
    three = medias(".ttl-cast", "_watch_cast.css", "grid-template-columns", "repeat(3")
    assert three
    narrowest_four = max(px(re.search(r"max-width: ([\d.]+px)", w[-1]).group(1)) for w in three)
    assert narrowest_four >= 519


def test_a_long_role_is_cut_to_two_lines_on_a_person_whose_card_shows_it_whole_on_touch_too() -> (
    None
):
    clamped = [
        where
        for where, sels, decls in rules("_watch_cast.css")
        if "button.ttl-cast__item > .ttl-cast__char" in sels
        and decls.get("-webkit-line-clamp") == "2"
    ]
    assert () in clamped
