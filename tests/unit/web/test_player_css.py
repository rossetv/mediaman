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
    assert [s for s, _ in rules] == [
        ".player.is-held.is-loading:not(.is-switching) .player-spinner"
    ]
    assert re.fullmatch(r"\s*transition-delay:\s*\.6s;\s*", rules[0][1])


def test_what_has_played_lies_over_the_bands_and_the_gaps_over_both() -> None:
    css = _text("_player.css")

    def level(selector: str) -> int:
        m = re.search(r"z-index:\s*(\d+)", _rule(css, selector))
        return int(m.group(1)) if m else 0

    assert level(".player-band") < level(".player-seek-played") < level(".player-gap")
    assert level(".player-gap") <= level(".player-seek-thumb")


def test_an_intro_or_credits_band_is_a_lighter_stretch_of_the_bar_cut_off_at_its_ends() -> None:
    css = _text("_player.css")
    band = _rule(css, ".player-band")

    def share(selector: str) -> float:
        m = re.search(r"var\(--t1\) (\d+)%, transparent\)", _rule(css, selector))
        assert m, selector
        return int(m.group(1)) / 100

    def over(level: float, part: float) -> float:
        return level + (255 - level) * part

    fill = share(".player-band")
    for frame in (0, 45, 90):  # the bar's shade leaves no brighter frame under it
        track = over(frame, share(".player-seek-track::before"))
        buffered = over(track, share(".player-seek-buffered"))
        assert _contrast(over(track, fill), track) >= 1.5, frame
        assert _contrast(over(buffered, fill), buffered) >= 1.2, frame
    assert _contrast(over(255 * 0.2, fill), over(255 * 0.2, share(".player-seek-buffered"))) >= 1.3
    # The cuts are dark all through, not a tint over the fill.
    assert "border: solid var(--bg)" in band and "border-width: 0 2px" in band
    assert "padding-box" in band
    assert not re.search(r"\b(top|bottom|height|outline|box-shadow|z-index):", band)


def test_a_pill_s_focus_ring_follows_the_pill_and_not_the_taller_box_it_is_pressed_by() -> None:
    css = _text("_player.css")
    assert re.search(r"outline:\s*none", _rule(css, ".player-pill:focus-visible"))
    assert re.search(r"outline:\s*2px solid", _rule(css, ".player-pill:focus-visible::before"))
    # Forced colours draw outlines, so nothing there may take it away again.
    forced = css[css.index("@media (forced-colors: active)") :]
    assert "outline: none" not in forced


def test_under_reduced_motion_the_spinner_over_a_held_frame_comes_at_once() -> None:
    css = _text("_player.css")
    moving = "".join(
        re.findall(r"@media \(prefers-reduced-motion: no-preference\) \{(.*?)\n\}", css, re.S)
    )
    held = ".is-held.is-loading:not(.is-switching) .player-spinner { transition-delay"
    assert (moving.count(held), css.count(held)) == (1, 1)


def test_the_question_over_the_credits_stands_clear_of_the_skip_button_on_a_phone() -> None:
    css = _text("_player.css")
    phone = css[css.index("@media (max-width: 600px) and (min-height: 600px)") :]
    skip = re.search(
        r"\.player-skip \{ right: 12px; bottom: calc\(var\(--player-foot\) \+ (\d+)", css
    )
    card = re.search(
        r"\.player-rate--beside \{[^}]*bottom: calc\(var\(--player-foot\) \+ (\d+)", phone
    )
    assert skip and card
    assert int(card.group(1)) >= int(skip.group(1)) + 44


def test_the_one_row_question_has_room_for_its_words_beside_the_next_episode_on_a_phone() -> None:
    css = _text("_player.css")
    block = css[
        css.index(
            "@media (min-width: 600.02px) and (max-width: 760px) and (max-height: 520px) and (orientation: landscape)"
        ) :
    ]
    taken = re.search(
        r"\.player-upnext[^{]*\.player-rate--beside \{[^}]*calc\(100% - (\d+)px\)", block
    )
    assert taken
    # The four thumbs in their frame, the close button, two gaps and the card's edges.
    assert 660 - int(taken.group(1)) - 188 - 44 - 2 * 8 - 18 >= 100


def test_the_end_of_a_title_on_a_tv_leaves_only_what_may_be_done_next() -> None:
    css = _text("_player.css")
    gone = ".player.is-ended .player-seek-row, .player.is-ended .player-controls > :not(.player-away-end)"
    rules = {" ".join(s.split()): b for s, b in re.findall(r"([^{}]+)\{([^{}]*)\}", css)}
    assert "display: none" in rules[gone]


def test_next_is_named_and_stands_after_the_quality_on_a_tvs_remote() -> None:
    css = _text("_player.css")
    assert "'Next'" in _rule(css, ".player.is-away [data-next-btn]::after")
    order = [
        int(re.search(r"order: (\d+)", _rule(css, s)).group(1))
        for s in (".player.is-away [data-next-btn]", ".player.is-away .player-away-end")
    ]
    assert order == sorted(order) == [1, 2]
    phone = css[css.index("\n@media (max-width: 899.98px) {\n  .player.is-away") :]
    row = [
        int(re.search(rf"{re.escape(s)} \{{ order: (\d+)", phone).group(1))
        for s in (
            ".player.is-away :is(.player-opt, .player-chapters)",
            ".player.is-away [data-next-btn]",
            ".player.is-away .player-brk--3",
            ".player.is-away .player-away-end",
        )
    ]
    assert row == sorted(set(row))


def test_the_menus_of_a_tvs_remote_stay_inside_a_low_screen() -> None:
    css = _text("_player.css")
    menus = re.findall(r"\.player\.is-away \.player-bottom \.player-menu \{([^}]*)\}", css)
    assert len(menus) == 2
    for body in menus:
        foot = int(re.search(r"bottom: (\d+)px", body).group(1))
        taken = int(re.search(r"max-height: calc\(100dvh - (\d+)px\)", body).group(1))
        assert taken >= foot + 12


def test_the_page_under_a_tvs_remote_is_laid_out_as_the_remote() -> None:
    # Hidden and not gone, the rows keep the picture where the remote has it.
    css = _text("_player.css")
    rules = {" ".join(s.split()): b for s, b in re.findall(r"([^{}]+)\{([^{}]*)\}", css)}
    kept = ".player.is-on-tv .player-seek-row, .player.is-on-tv .player-controls > :not(.player-away-end)"
    assert "visibility: hidden" in rules[kept]
    stacked = rules[".player.is-on-tv .player-away-end [data-open-remote]:not([hidden])"]
    assert "flex: 1 1 100%" in stacked
    assert "flex-wrap: wrap" in re.search(r"\.player-away-end \{ order: 10;[^}]*", css).group(0)


def test_try_again_in_the_sheet_stands_out_from_its_box() -> None:
    css = _text("_play_on.css")
    tokens = _text("_tokens.css")

    def grey(body: str) -> int:
        name = re.search(r"background:[^;]*var\(--(s\d)\)", body).group(1)
        return int(re.search(rf"--{name}:\s*#(..)", tokens).group(1), 16)

    box = grey(_rule(css, ".play-on__say"))
    assert _contrast(grey(_rule(css, ".play-on__say .btn--secondary")), box) >= 1.2
