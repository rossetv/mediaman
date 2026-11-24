"""The player's stylesheet: text stays readable over a bright picture, and toasts and buttons
are never lost in it.

These read the stylesheet rules, since no layout engine runs here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS_DIR = Path(__file__).resolve().parents[3] / "src" / "mediaman" / "web" / "static" / "css"


def _text(name: str) -> str:
    return re.sub(r"/\*.*?\*/", "", (CSS_DIR / name).read_text(encoding="utf-8"), flags=re.S)


def _rule(css: str, selector: str) -> str:
    """The body of the first rule whose selector list is exactly `selector`."""
    m = re.search(rf"(?:^|[}}{{])\s*{re.escape(selector)}\s*\{{([^}}]*)\}}", css)
    assert m, f"no rule {selector}"
    return m.group(1)


def test_toasts_on_the_player_leave_its_bottom_controls_alone() -> None:
    # Wide pages and the phone Watch page send the stack to the bottom right, on the controls.
    css = _text("_ui_feedback.css")
    player = _rule(css, "body:has(.player) .ui-toast-host")
    assert re.search(r"(?<![\w-])bottom:\s*auto", player)
    assert re.search(r"(?<![\w-])right:\s*auto", player)
    assert re.search(r"(?<![\w-])top:\s*var\(--toast-top\)", player)


def test_toasts_on_a_phone_leave_the_open_keep_menu_alone() -> None:
    css = _text("_ui_feedback.css")
    host = _rule(css, 'body:has(.player-keep [aria-expanded="true"]) .ui-toast-host')
    # Back row 44, Keep row 44, the menu's three 44 px choices and the gaps between them.
    assert int(re.search(r"\+ (\d+)px", host).group(1)) >= 44 + 8 + 44 + 4 + 3 * 44 + 12


def _luminance(level: float) -> float:
    c = level / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _contrast(a: float, b: float) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize(
    ("bar", "row", "reach"),
    [
        # The subtitle ends 54 px into the top bar; the times end 82 px up the bottom one.
        (".player-top", ".player-subtitle", "var(--player-top) + 54px"),
        (".player-bottom", ".player-seek-row", "var(--player-foot) + 82px"),
    ],
)
def test_text_over_a_white_frame_keeps_aa_contrast(bar: str, row: str, reach: str) -> None:
    css = _text("_player.css")
    stops = re.search(r"linear-gradient\((.*)\);", _rule(css, bar)).group(1)
    assert f"calc({reach})" in stops
    scrims = [int(p) for p in re.findall(r"var\(--scrim\) (\d+)%", stops)]
    assert len(scrims) == 2
    frame = 255 * (1 - min(scrims) / 100)
    token = re.search(r"color:\s*var\(--([\w-]+)\)", _rule(css, row)).group(1)
    share = int(re.search(rf"--{token}:[^;]*?(\d+)%", css).group(1)) / 100
    assert _contrast(255 * share + frame * (1 - share), frame) >= 4.5


def test_a_quiet_button_in_a_card_stands_out_from_the_card() -> None:
    css = _text("_player.css")
    tokens = _text("_tokens.css")

    def grey(body: str) -> int:
        name = re.search(r"background:[^;]*var\(--(s\d)\)", body).group(1)
        return int(re.search(rf"--{name}:\s*#(..)", tokens).group(1), 16)

    pill = _rule(css, ".player-card .player-pill:not(.player-pill--primary)::before")
    assert _contrast(grey(pill), grey(_rule(css, ".player-card"))) >= 1.4
