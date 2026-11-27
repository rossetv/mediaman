"""The Profile pane's stylesheet (_profile.css): how its hub and views feel under a finger and a pointer."""

from __future__ import annotations

import re
from pathlib import Path

CSS = re.sub(
    r"/\*.*?\*/",
    "",
    (Path(__file__).resolve().parents[3] / "src/mediaman/web/static/css/_profile.css").read_text(),
    flags=re.S,
)
TOP = re.sub(r"@media[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", CSS)


def _block(at: str) -> dict[str, str]:
    start = CSS.index(at + " {") + len(at) + 2
    depth, i = 1, start
    while depth:
        depth += {"{": 1, "}": -1}.get(CSS[i], 0)
        i += 1
    return _rules(CSS[start : i - 1])


def _rules(css: str) -> dict[str, str]:
    return {sel.strip(): body for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)}


def test_a_hover_lights_a_row_only_where_there_is_a_pointer_to_hover_with() -> None:
    assert ":hover" not in TOP
    hover = _block("@media (hover: hover)")
    assert any(".yo-row:hover" in sel for sel in hover)
    assert any(".yo-tile:hover" in sel for sel in hover)


def test_a_row_a_tile_and_log_out_answer_a_press_at_once() -> None:
    rules = _rules(TOP)
    for sel in (":is(a, button).yo-row:active", ".yo-tile:active", ".yo-out__btn:active"):
        assert "background" in rules[sel], sel
        # lit at once, eased off when let go
        assert "transition-duration: 0s" in rules[sel], sel


def test_a_long_name_stops_at_two_lines_with_its_whole_self_in_its_title() -> None:
    name = _rules(TOP)[".yo-name"]
    assert "-webkit-line-clamp: 2" in name and "overflow: hidden" in name
    assert "clamp(" in re.search(r"font: ([^;]*);", name).group(1)


def test_on_a_short_screen_the_save_button_follows_the_form_and_covers_nothing() -> None:
    short = _block("@media (max-height: 500px)")
    assert "position: static" in short[".yo-foot"]


def test_the_hub_tiles_headings_sit_on_one_line_whatever_their_subtitles() -> None:
    assert "align-content: start" in _rules(TOP)[".yo-tile"]


def test_a_save_that_cannot_be_pressed_yet_looks_it() -> None:
    off = _rules(TOP)[".yo-foot .btn--primary:disabled"]
    assert "background: var(--s3)" in off and "color: var(--t3)" in off
