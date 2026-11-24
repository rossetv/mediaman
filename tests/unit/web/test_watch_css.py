"""What the Watch pages' stylesheet has to hold for its badges and toggles to be seen, read as rules."""

from __future__ import annotations

import re
from pathlib import Path

SHEET = Path(__file__).resolve().parents[3] / "src/mediaman/web/static/css/_watch.css"
FORCED = "(forced-colors: active)"


def _of(selector: str, media: str | None = None) -> str:
    """The declarations of *selector* outside any `@media`, or inside those asking for *media*."""
    css = re.sub(r"/\*.*?\*/", "", SHEET.read_text(encoding="utf-8"), flags=re.S)
    blocks = r"@media ([^{]+)\{((?:[^{}]*\{[^{}]*\})*)\s*\}"
    text = (
        "".join(body for query, body in re.findall(blocks, css) if media in query)
        if media
        else re.sub(blocks, "", css)
    )
    return "".join(
        body
        for head, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text)
        if selector in [s.strip() for s in head.split(",")]
    )


def test_forced_colours_paint_a_watched_toggle_s_ring_and_not_all_it_takes_to_press() -> None:
    assert "background: Highlight" in _of('.watch-mark[aria-pressed="true"]::before', FORCED)
    assert "background" not in _of('.watch-mark[aria-pressed="true"]', FORCED)
    assert "background: Highlight" in _of('.watch-mark-btn[aria-pressed="true"]', FORCED)


def _background(selector: str) -> str:
    return _of(selector).split("background:")[1].split(";")[0].strip()


def test_a_leaving_badge_is_dark_and_turns_red_only_when_it_leaves_now() -> None:
    dark = re.fullmatch(
        r"color-mix\(in srgb, var\(--scrim\) (\d+)%, transparent\)",
        _background(".watch-badge--leaving"),
    )
    # White on black this dense over a white poster is 4.5:1 from 55%.
    assert dark and int(dark.group(1)) >= 55
    red = _background(".watch-badge--leaving-now")
    assert "var(--danger)" in red and "transparent" not in red
    css = SHEET.read_text(encoding="utf-8")
    assert css.index(".watch-badge--leaving-now {") > css.index(".watch-badge--leaving {")


def test_a_leaving_badge_has_an_edge_in_forced_colours() -> None:
    assert "outline: 1px solid CanvasText" in _of(".watch-badge--leaving", FORCED)
