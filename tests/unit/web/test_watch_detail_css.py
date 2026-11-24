"""What the title detail overlay's stylesheet has to hold for its parts to fit, stay reachable
and stay put, read as rules: selectors and declarations, per media query."""

from __future__ import annotations

import re
from pathlib import Path

CSS = Path(__file__).resolve().parents[3] / "src/mediaman/web/static/css/_watch_detail.css"
PHONE = "(max-width: 699.98px)"


def _rules(media: str | None = None) -> list[tuple[list[str], str]]:
    """The (selectors, declarations) of the sheet outside any `@media`, or of those inside every
    `@media` whose query holds *media*."""
    css = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)
    blocks = re.findall(r"@media ([^{]+)\{((?:[^{}]*\{[^{}]*\})*)\s*\}", css)
    text = (
        "".join(body for query, body in blocks if media in query)
        if media
        else re.sub(r"@media [^{]+\{(?:[^{}]*\{[^{}]*\})*\s*\}", "", css)
    )
    return [
        ([s.strip() for s in head.split(",")], body)
        for head, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text)
    ]


def _of(selector: str, media: str | None = None) -> str:
    return "".join(body for selectors, body in _rules(media) if selector in selectors)


def test_a_phone_shows_which_episodes_are_watched_or_in_progress() -> None:
    assert "display: none" not in _of(".ttl .watch-ep__state", PHONE)
    assert '". thumb state"' in _of(".ttl .watch-ep", PHONE)


def test_a_long_season_name_wraps_inside_its_chip() -> None:
    assert "max-width: 100%" in _of(".season-chip")
    assert "min-width: 0" in _of(".season-chip__name")
    phone = _of(".season-chip", PHONE)
    assert "nowrap" not in phone and "min-width: 0" in phone


def test_a_long_cast_name_wraps_inside_its_own_column() -> None:
    body = _of(".ttl-cast__name")
    assert "overflow-wrap: anywhere" in body and "max-width: 100%" in body


def test_a_trailer_s_bar_stands_over_the_video_and_not_on_it() -> None:
    assert "padding: var(--ttl-bar) 0" in _of(".ttl__player")
    assert "height: var(--ttl-bar)" in _of(".ttl__bar")


def test_a_trailer_s_frame_never_stands_taller_than_its_box() -> None:
    body = _of(".ttl__frame")
    assert "width: 100%" in body and "height: 100%" in body and "aspect-ratio" not in body


def test_close_stays_on_screen_on_a_phone_either_way_up() -> None:
    assert "position: sticky" in _of(".ttl__bar", "(max-width: 699.98px), (max-height: 500px)")
    # A scroll container of its own would hold the bar at the sheet's top instead.
    assert re.search(r"overflow:\s*clip", _of(".ttl__sheet"))
