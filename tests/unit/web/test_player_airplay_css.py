"""While a TV plays, Safari sends it only what the page shows, so the AirPlay video has to stay
rendered under `.player.is-away`. Read as rules: no `display: none` reaches it."""

from __future__ import annotations

import re
from pathlib import Path

SHEET = Path(__file__).resolve().parents[3] / "src/mediaman/web/static/css/_player.css"


def _rules() -> list[tuple[str, str]]:
    css = re.sub(r"/\*.*?\*/", "", SHEET.read_text(encoding="utf-8"), flags=re.S)
    return re.findall(r"([^{}]+)\{([^{}]*)\}", css)


def test_the_airplay_video_is_not_among_what_away_mode_hides() -> None:
    hiding = [
        head
        for head, body in _rules()
        if ".is-away" in head and re.search(r"display:\s*none\s*!important", body)
    ]
    assert hiding
    for head in hiding:
        for plain in re.findall(r"\.player-video(?!:not\(\.player-air\))(?![\w-])", head):
            raise AssertionError(f"hides the AirPlay video too: {head.strip()} ({plain})")


def test_away_mode_shows_the_airplay_video_out_of_sight() -> None:
    shown = "".join(
        body for head, body in _rules() if head.strip() == ".player.is-away .player-air"
    )
    assert "display: block" in shown
    assert "opacity: 0" in shown
