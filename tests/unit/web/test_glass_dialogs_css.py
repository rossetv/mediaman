"""Glass on the dialogs: the confirm card, the shared modal sheet (abandon a download, the password
check), the keep dialogs and the sheet of TVs to play on, over a plain dim that lets the glass show the page, faded on the glass
itself, legible over a white page, and solid on a phone where glass is already open under them."""

from __future__ import annotations

import re

import pytest

from tests.unit.web._css import contrast, rules, style, text, token
from tests.unit.web.test_glass_css import (
    BLACK,
    PHONE,
    WHITE,
    _colour,
    _glass_over,
    _on,
    _over,
    _resolve,
)

CONFIRM = "_ui_feedback.css"
MODAL = "_modal.css"
ASK = "_watch_ask.css"
WATCH_LIST = "_watch_list.css"
PLAY_ON = "_play_on.css"
BUTTONS = "_buttons.css"
# A phone on its side: short, and touched.
SIDEWAYS = "(max-height: 500px) and (pointer: coarse)"

CARDS = [
    (".ui-confirm-card", CONFIRM),
    (".modal-sheet", MODAL),
    (".keep-dialog", MODAL),
    (".play-on", PLAY_ON),
]
DIMS = [
    (".ui-confirm-overlay::before", CONFIRM),
    (".modal-backdrop", MODAL),
    (".keep-dialog-overlay", MODAL),
]
WORDS = ("--t2", "--t3", "--t4", "--accent-text", "--accent-hi", "--danger-text")


@pytest.mark.parametrize(("card", "sheet"), CARDS)
def test_each_dialog_is_glass_with_the_same_filter_in_every_engine(card: str, sheet: str) -> None:
    drawn = style(card, sheet)
    assert drawn["background"] == "var(--g-regular)"
    # Safari reads only the prefixed one; a different value would be another material there.
    assert drawn["backdrop-filter"] == drawn["-webkit-backdrop-filter"] == "var(--g-filter)"
    assert drawn["border"] == "1px solid var(--g-edge)"
    assert drawn["border-top-color"] == "var(--g-lit)"


@pytest.mark.parametrize(("card", "sheet"), CARDS)
def test_every_word_on_a_dialog_takes_a_colour_lifted_for_its_glass(card: str, sheet: str) -> None:
    declared = style(card, sheet)
    glass = _glass_over(WHITE, float(token("--g-a")), filtered=True)
    for name in WORDS:
        assert name in declared, f"{card} leaves {name} at its colour for solid grey"
        assert contrast(_on(_resolve(declared[name]), glass), glass) >= 4.5, (card, name)
    # A fill on it is a veil of the glass, never the solid grey that would hide it.
    for step in ("--s1", "--s2", "--s3", "--s4", "--s5"):
        assert declared[step] == f"var(--g{step[1:]})", (card, step)


@pytest.mark.parametrize(("dim", "sheet"), DIMS)
def test_the_page_under_a_dialog_is_a_plain_dim_that_blurs_nothing(dim: str, sheet: str) -> None:
    # A blurred page under glass is a second blur for nothing, and the glass has no page to show.
    assert style(dim, sheet)["background"] == "var(--g-dim)"
    owner = dim.removesuffix("::before")
    for _, sels, decls in rules(sheet):
        if any(s == owner or s.startswith(owner + "::") for s in sels):
            assert "backdrop-filter" not in decls, sels


def test_the_confirm_fades_its_dim_and_its_card_and_never_the_overlay_they_lie_in() -> None:
    # Under a fading parent, Chrome's glass sees none of the page until the fade ends.
    for _, sels, decls in rules(CONFIRM):
        for sel in sels:
            if re.fullmatch(r"\.ui-confirm-overlay(?:\.[\w-]+|\[[\w-]+\])*", sel):
                assert "opacity" not in decls and "animation" not in decls, sel
    # Each comes in from nothing.
    for faded in (".ui-confirm-overlay::before", ".ui-confirm-card"):
        assert style(faded, CONFIRM)["opacity"] == "0", faded
    assert style(".ui-confirm-overlay.is-visible::before", CONFIRM)["opacity"] == "1"
    assert style(".ui-confirm-overlay.is-visible .ui-confirm-card", CONFIRM)["opacity"] == "1"
    # Both on the overlay's timing, which ui-feedback.js reads as it closes: a timing that moves
    # opacity, over a time, or both would snap and the script would wait on its fallback.
    timing = style(".ui-confirm-overlay", CONFIRM)["transition"].split()
    assert timing[0] == "opacity" and timing[1] not in ("0", "0s"), timing
    assert style(".ui-confirm-overlay::before", CONFIRM)["transition"] == "inherit"
    card = style(".ui-confirm-card", CONFIRM)
    assert card["transition"] == "inherit"
    assert "opacity" in card.get("transition-property", "opacity").replace(" ", "").split(",")


def test_the_sheet_of_tvs_lies_on_a_plain_dim_on_a_phone_and_on_nothing_wider() -> None:
    assert style(".play-on::backdrop", PLAY_ON, PHONE)["background"] == "var(--g-dim)"
    assert style(".play-on::backdrop", PLAY_ON)["background"] == "none"
    for _, sels, decls in rules(PLAY_ON):
        if ".play-on::backdrop" in sels:
            assert "backdrop-filter" not in decls and "-webkit-backdrop-filter" not in decls


def test_a_modal_brings_its_dim_in_and_out_and_fades_only_its_sheet() -> None:
    frames = dict(re.findall(r"@keyframes ([\w-]+) \{(.*?)\}\n", text(MODAL) + text(WATCH_LIST)))
    assert "wl-dialog-scrim" in style(".wl-dialog.active", WATCH_LIST)["animation"]
    for name in ("modal-in", "modal-out", "wl-dialog-scrim"):
        assert "opacity" not in frames[name] and "background-color" in frames[name], name
    for name in ("modal-sheet-in", "modal-sheet-out"):
        assert "opacity: 0" in frames[name], name
    assert "modal-in" in style(".modal-backdrop", MODAL)["animation"]
    # Each sheet fades itself in and out: a dim's keyframes there would only move its background.
    for moving, name in (
        (".modal-backdrop > .modal-sheet", "modal-sheet-in"),
        (".keep-dialog-overlay.active > .keep-dialog", "modal-sheet-in"),
        (".modal-backdrop.is-closing > .modal-sheet", "modal-sheet-out"),
        (".keep-dialog-overlay.active.is-closing > .keep-dialog", "modal-sheet-out"),
    ):
        assert style(moving, MODAL)["animation"].split()[0] == name, moving


@pytest.mark.parametrize(
    ("over", "card", "sheet"),
    [
        ("html:is(.has-ttl, .has-space) .ui-confirm-card", ".ui-confirm-card", CONFIRM),
        (".you.is-open ~ .ui-confirm-overlay .ui-confirm-card", ".ui-confirm-card", CONFIRM),
        ("html:is(.has-ttl, .has-space) :is(.modal-sheet, .keep-dialog)", ".modal-sheet", MODAL),
        (".you.is-open ~ * :is(.modal-sheet, .keep-dialog)", ".modal-sheet", MODAL),
        ("html:is(.has-ttl, .has-tv-rem) .play-on", ".play-on", PLAY_ON),
    ],
)
@pytest.mark.parametrize("phone", [PHONE, SIDEWAYS])
def test_on_a_phone_a_dialog_over_glass_is_solid_so_no_third_layer_blurs(
    over: str, card: str, sheet: str, phone: str
) -> None:
    drawn = style(over, sheet, phone)
    assert drawn["background"] == "var(--g-opaque)"
    assert drawn["backdrop-filter"] == drawn["-webkit-backdrop-filter"] == "none"


def test_the_fill_of_a_dialog_with_no_blur_is_opaque_so_no_word_behind_it_shows() -> None:
    # A see-through tint without its blur lets the bright words of a form under it be read.
    fill = token("--g-opaque")
    assert fill.startswith("rgb(") and "/" not in fill


def test_the_download_mark_on_an_answer_s_poster_is_a_solid_chip_that_blurs_nothing() -> None:
    drawn = style(".watch-badge--get", ASK)
    assert drawn["background"] == "var(--g-tip)"
    # A light hairline round it, or a dark poster swallows the chip's shape.
    assert re.fullmatch(r"0 0 0 1px rgb\(255 255 255 / \.\d+\)", drawn["box-shadow"])
    assert "backdrop-filter" not in drawn and "-webkit-backdrop-filter" not in drawn
    assert contrast((255.0, 255.0, 255.0), _on(token("--g-tip"), WHITE)) >= 4.5


@pytest.mark.parametrize(("card", "sheet"), CARDS)
def test_a_dialog_s_rings_and_ticks_hold_3_to_1_over_its_glass_on_a_white_page(
    card: str, sheet: str
) -> None:
    # The page's #2997ff is near the glass's own tone over a white page; Play on's rings, its
    # code box and its "on" tick take --accent.
    declared = style(card, sheet)
    glass = _glass_over(WHITE, float(token("--g-a")), filtered=True)
    assert "--accent" in declared, f"{card} leaves --accent at the page's blue"
    assert contrast(_over(_colour(declared["--accent"], {}), glass), glass) >= 3
    assert contrast(_over(_colour("var(--g-focus)", {}), glass), glass) >= 3


@pytest.mark.parametrize(
    ("ring", "sheet"),
    [
        (":is(.modal-sheet, .keep-dialog) :focus-visible", MODAL),
        (".ui-confirm-card :focus-visible", CONFIRM),
    ],
)
def test_the_keyboard_s_ring_on_a_dialog_is_the_one_lifted_for_glass(ring: str, sheet: str) -> None:
    assert style(ring, sheet)["outline-color"] == "var(--g-focus)"


DISABLED = ':is(.ui-confirm-card, .modal-sheet, .keep-dialog, .play-on, .you__panel) .btn:is(:disabled, [aria-disabled="true"])'


@pytest.mark.parametrize(("card", "sheet"), [*CARDS, (".you__panel", "_you.css")])
@pytest.mark.parametrize("page", [WHITE, BLACK])
def test_a_button_that_cannot_be_pressed_yet_on_a_dialog_still_reads_4_5_to_1(
    card: str, sheet: str, page: tuple[float, ...]
) -> None:
    # A faded button lets the glass through its words: over a white page they fall under 4.5:1.
    assert card.lstrip(".") in DISABLED
    drawn = style(DISABLED, BUTTONS)
    assert drawn["opacity"] == "1"
    scope = style(card, sheet)
    glass = _glass_over(page, float(token("--g-a")), filtered=True)
    fill = _over(_colour(drawn["background"], scope), glass)
    words = _over(_colour(drawn["color"], scope), fill)
    assert contrast(words, fill) >= 4.5
    # Quieter than what can be pressed, so it still reads as not yet.
    assert _colour(drawn["color"], scope)[3] < 1


def test_a_button_that_cannot_be_pressed_yet_does_not_shrink_when_pressed() -> None:
    press = style('.btn:active:not(:disabled, [aria-disabled="true"])', BUTTONS)
    assert press["transform"] == "scale(.97)"


def test_save_email_is_quiet_when_aria_disabled_as_when_disabled() -> None:
    assert '.yo-foot .btn--primary:is(:disabled, [aria-disabled="true"])' in {
        s for _, sels, _ in rules("_profile.css") for s in sels
    }


def test_a_button_that_cannot_be_pressed_yet_reads_so_in_forced_colours_too() -> None:
    assert style(DISABLED, BUTTONS, "(forced-colors: active)")["color"] == "GrayText"


def test_no_fill_on_a_dialog_is_the_step_that_glass_turns_to_nothing() -> None:
    # On glass --s1 is transparent: a well, a row or a box in it would vanish.
    for fill, sheet in (
        (".keep-duration-bar", MODAL),
        (".keep-season-item", MODAL),
        (".play-on__code i", PLAY_ON),
    ):
        assert style(fill, sheet)["background"] != "var(--s1)", fill
