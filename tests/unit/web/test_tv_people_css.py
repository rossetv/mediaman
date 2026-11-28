"""The people a TV is paired with, as the TV and the pages show them: big enough to read from a
sofa, in colours that hold 4.5:1, rung in the glass's focus colour, and still under reduced motion."""

from __future__ import annotations

import re

import pytest

from tests.unit.web._css import _within, contrast, px, rgb, rules, style, text, token

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


def test_the_tvs_focus_ring_is_the_glass_s_focus_colour_and_reads_on_the_dark_around_it() -> None:
    assert token("--g-focus") == "var(--g-accent-text)"
    focused = style(".b.f", TV)
    ring = focused["box-shadow"]
    assert ring.endswith(token("--g-accent-text"))
    # It stands outside the white fill, past a dark gap, on the page or the film's dark shade.
    assert px(ring.split()[3]) >= 4 and focused["border-color"] == "#000"
    for under in ("#000", "#1d1d1f", focused["border-color"]):
        assert contrast(rgb(token("--g-accent-text")), rgb(under)) >= 3


def test_the_tv_moves_on_the_app_s_curves_and_its_cards_are_the_glass_s_solid_weight() -> None:
    """The TV's sheet cannot read the tokens, so it copies them; these keep the copies true."""
    curves = {token(name).replace(" ", "") for name in ("--ease-out", "--ease-push", "--ease-exit")}
    used = {c.replace(" ", "") for c in re.findall(r"cubic-bezier\([^)]*\)", text(TV))}
    assert used and used <= curves
    tint = "rgba({}, .94)".format(", ".join(token("--g-tint").split()))
    edge = token("--g-edge").replace("rgb(255 255 255 / ", "rgba(255, 255, 255, ")
    lit = token("--g-lit").replace("rgb(255 255 255 / ", "rgba(255, 255, 255, ")
    for card in (".up", ".sk"):
        glass = style(card, TV)
        assert edge in glass["border"] and glass["border-top-color"] == lit, card
    assert style(".up", TV)["background"] == tint


def _sets(decls: dict[str, str], prop: str) -> bool:
    return prop in decls or f"-webkit-{prop}" in decls


@pytest.mark.parametrize("prop", ["animation", "transition"])
def test_nothing_on_the_tv_moves_under_reduced_motion(prop: str) -> None:
    """The cascade under reduced motion: for each selector that moves, the last rule to say how
    holds still, whatever order the sheet keeps."""
    last: dict[str, tuple[bool, dict[str, str]]] = {}
    for where, selectors, decls in rules(TV):
        reduced = _within(where, REDUCED)
        if (where and not reduced and not where[0].startswith("@supports")) or not _sets(
            decls, prop
        ):
            continue
        for selector in selectors:
            last[selector] = (reduced, decls)
    moving = {sel for sel, (reduced, decls) in last.items() if not reduced}
    assert not moving, f"{prop} left on under reduced motion: {sorted(moving)}"
    for selector, (_, decls) in last.items():
        held = decls.get(prop, decls.get(f"-webkit-{prop}", ""))
        # The spinner pulses in place where it would turn.
        assert held == "none" or held.startswith("pu "), (selector, held)


def test_on_the_pages_a_letter_lies_under_its_picture_and_shows_where_it_fails() -> None:
    picture = style(".avatar--pic img", "_nav.css")
    assert (picture["position"], picture["inset"]) == ("absolute", "0")
    assert style(".avatar--pic", "_nav.css")["position"] == "relative"
    assert style('img[data-broken=""]', "_base.css")["display"] == "none"
    # A picture that failed leaves its letter on the disc of one who has none.
    broken = style('.avatar--pic:has(> .avatar__letter + img[data-broken=""])', "_nav.css")
    assert broken["background"] == style(".avatar--letter", "_nav.css")["background"]


def test_the_pair_page_rings_its_code_in_the_glass_s_focus_colour() -> None:
    ring = style(".pair__code:focus-within i.at", "_pair.css")
    assert ring["outline"].endswith("var(--g-focus)")
