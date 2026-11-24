"""What the Watch pages' stylesheet has to hold for its badges and toggles to be seen, read as rules."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SHEET = Path(__file__).resolve().parents[3] / "src/mediaman/web/static/css/_watch.css"
FORCED = "(forced-colors: active)"


def _of(selector: str, media: str | None = None, sheet: Path = SHEET) -> str:
    """The declarations of *selector* outside any `@media`, or inside those asking for *media*."""
    css = re.sub(r"/\*.*?\*/", "", sheet.read_text(encoding="utf-8"), flags=re.S)
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


def test_a_leaving_badge_says_the_days_left_at_every_width() -> None:
    assert "display: none" in _of(".watch-badge__long")
    assert "display: none" not in _of(".watch-badge__short")
    assert not any(_of(s, "width") for s in (".watch-badge__long", ".watch-badge__short"))


@pytest.mark.parametrize(
    "seen",
    [
        ".watch-badge--watched",
        ".watch-pill__mark",
        '.watch-mark[aria-pressed="true"]',
        '.watch-mark-btn[aria-pressed="true"]',
    ],
)
def test_the_eye_of_what_is_watched_is_another_one(seen: str) -> None:
    assert _of(seen).count("--eye-open: none; --eye-seen: inline;") == 1
    assert _of(".eye-open").strip() == "display: var(--eye-open, inline);"
    assert _of(".eye-seen").strip() == "display: var(--eye-seen, none);"
    sprite = (SHEET.parents[2] / "templates/_icons.html").read_text(encoding="utf-8")
    eye = re.search(r'<symbol id="i-eye".*?</symbol>', sprite)
    assert eye and 'class="eye-open"' in eye[0] and 'class="eye-seen" display="none"' in eye[0]


def test_a_watched_toggle_is_filled_far_more_than_one_that_is_not() -> None:
    fill = re.search(r"var\(--t1\) (\d+)%", _background('.watch-mark[aria-pressed="true"]::before'))
    assert fill and int(fill[1]) >= 80
    assert "color: var(--bg)" in _of('.watch-mark[aria-pressed="true"]')
    assert "background" not in _of(".watch-mark::before").replace("background-color", "")


FORCED_CHOICES = [
    (SHEET, '.watch-viewtoggle__btn[aria-current="page"]::before'),
    (SHEET, ".watch-pill.on"),
    (SHEET.with_name("_buttons.css"), ".fpill.on"),
    (SHEET.with_name("_dl.css"), ".dl-bar-fill"),
    (SHEET.with_name("_dl.css"), ".dl-hero-fill"),
    (SHEET.with_name("_dl.css"), ".dl-row-bar-fill"),
    (SHEET.with_name("_dl.css"), ".dl-compact-fill"),
    (SHEET.with_name("_dl.css"), ".dl-ep-mini-fill"),
]


@pytest.mark.parametrize(("sheet", "selector"), FORCED_CHOICES)
def test_forced_colours_show_what_is_on_and_how_far_a_download_is(
    sheet: Path, selector: str
) -> None:
    assert "background: Highlight" in _of(selector, FORCED, sheet)


def test_forced_colours_give_keep_an_edge_and_a_hidden_picture_no_room() -> None:
    assert "border: 1px solid" in _of(".keep-trigger", FORCED, SHEET.with_name("_buttons.css"))
    assert "border: 1px solid" in _of(".watch-keep::before", FORCED)
    banner = _of(".watch-banner:has(> .watch-banner__bg--art:not([data-broken]))", FORCED)
    assert "min-height: 0" in banner
    detail = SHEET.with_name("_watch_detail.css")
    assert "min-height: 190px" in _of(".ttl:not(.is-trailer) .ttl__hero", FORCED, detail)


def test_a_leaving_badge_has_an_edge_in_forced_colours() -> None:
    assert "outline: 1px solid CanvasText" in _of(".watch-badge--leaving", FORCED)
