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


@pytest.mark.parametrize("track", ["::-webkit-slider-runnable-track", "::-moz-range-track"])
def test_the_volume_slider_draws_its_own_track(track: str) -> None:
    # Safari draws no track of its own on a dark page.
    css = _text("_player.css")
    assert re.search(r"(?<!-)appearance:\s*none", _rule(css, ".player-volume-slider"))
    body = _rule(css, f".player-volume-slider{track}")
    assert "height" in body and "background" in body


def test_a_long_menu_scrolls_inside_the_screen() -> None:
    menu = _rule(_text("_player.css"), ".player-menu")
    assert re.search(r"max-height:[^;]*100dvh", menu)
    assert re.search(r"overflow-y:\s*auto", menu)


def test_reduced_motion_is_not_overruled_by_a_later_rule() -> None:
    css = _text("_player.css")
    stilled = 0
    for block in re.finditer(r"@media \(prefers-reduced-motion: reduce\) \{(.*?)\n\}", css, re.S):
        for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", block.group(1)):
            if not re.search(r"(?<![\w-])animation:\s*none", body):
                continue
            for selector in re.split(r",\s*", selectors.strip()):
                stilled += 1
                for later, rule in re.findall(r"([^{}]+)\{([^{}]*)\}", css[block.end() :]):
                    if selector in re.split(r",\s*", later.strip()):
                        assert "animation:" not in rule, selector
    assert stilled


def test_the_skip_button_is_out_of_the_way_of_an_open_menu() -> None:
    css = _text("_player.css")
    hiding = next(
        (selectors, body)
        for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
        if '[aria-expanded="true"]' in selectors and ".player-skip" in selectors
    )
    assert re.search(r"visibility:\s*hidden", hiding[1])


def test_nothing_shows_through_a_menu() -> None:
    body = _rule(_text("_player.css"), ".player-menu")
    assert re.search(r"background:\s*var\(--s\d\)", body)


def test_the_spinner_over_a_held_frame_waits_for_a_new_quality_alone() -> None:
    css = _text("_player.css")
    rules = [
        (selectors.strip(), body)
        for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
        if ".player-spinner" in selectors and ".is-held" in selectors
    ]
    assert [s for s, _ in rules] == [".player.is-held.is-loading:not(.is-switching) .player-spinner"]
    assert re.fullmatch(r"\s*transition-delay:\s*\.6s;\s*", rules[0][1])
