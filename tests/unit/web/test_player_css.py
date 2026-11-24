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
