"""The people a TV is paired with, as the TV and the pages show them: big enough to read from a
sofa, in colours that hold 4.5:1, rung in the glass's focus colour, and still under reduced motion."""

from __future__ import annotations

import pytest

from tests.unit.web._css import contrast, px, rgb, style, token

TV = "tv.css"
REDUCED = "(prefers-reduced-motion: reduce)"
BLACK = (0.0, 0.0, 0.0)


def _brightest_page() -> tuple[float, ...]:
    """The TV page under its light at the strongest: the light's colour over black."""
    *colour, alpha = rgb("rgba(41, 151, 255, .18)")
    assert "rgba(41, 151, 255, .18)" in style(".page", TV)["background"]
    return tuple(c * alpha for c in colour)


@pytest.mark.parametrize(
    "selector",
    [".pn", ".l", ".sm", ".b", ".h", ".t2"],
)
def test_words_on_the_tv_are_big_enough_to_read_from_a_sofa(selector: str) -> None:
    assert px(style(selector, TV)["font-size"]) >= 24


@pytest.mark.parametrize(
    ("selector", "colour"),
    [(".pn", "color"), (".l", "color"), (".sm", "color"), (".who", "color")],
)
def test_words_on_the_tv_hold_their_contrast_under_its_light(selector: str, colour: str) -> None:
    words = rgb(style(selector, TV)[colour])
    assert contrast(words, _brightest_page()) >= 4.5


def test_a_persons_letter_holds_its_contrast_on_its_disc() -> None:
    letter = style(".av:before", TV)
    assert letter["content"] == "attr(data-l)"
    assert contrast(rgb(letter["color"]), rgb(letter["background"])) >= 4.5
    assert px(letter["font-size"]) >= 24
    assert px(style(".av--s:before", TV)["font-size"]) >= 24


def test_a_picture_that_failed_leaves_the_letter_under_it() -> None:
    assert style(".av img", TV)["position"] == "absolute"
    assert style(".av img.gone", TV)["display"] == "none"


def test_the_tvs_focus_ring_is_the_glass_s_focus_colour() -> None:
    assert token("--g-focus") == "var(--g-accent-text)"
    assert style(".b.f", TV)["border-color"] == token("--g-accent-text")


def test_the_people_rise_in_only_where_motion_is_wanted() -> None:
    assert "rise" in style(".pp", TV)["animation"]
    assert style(".pp", TV, REDUCED)["animation"] == "none"


def test_on_the_pages_a_letter_lies_under_its_picture_and_shows_where_it_fails() -> None:
    assert style(".avatar > .avatar__letter + img", "_nav.css")["position"] == "absolute"
    assert style('img[data-broken=""]', "_base.css")["display"] == "none"


def test_the_pair_page_rings_its_code_in_the_glass_s_focus_colour() -> None:
    ring = style(".pair__code:focus-within i.at", "_pair.css")
    assert ring["outline"].endswith("var(--g-focus)")
