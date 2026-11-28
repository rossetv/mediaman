"""Glass on the dialogs: the confirm card, the shared modal sheet (abandon a download, the password
check), the keep dialogs and the sheet of TVs to play on, over a plain dim that lets the glass show the page, faded on the glass
itself, legible over a white page, and solid on a phone where glass is already open under them."""

from __future__ import annotations

import re

import pytest

from tests.unit.web._css import contrast, rules, style, text, token
from tests.unit.web.test_glass_css import PHONE, WHITE, _glass_over, _on, _resolve

CONFIRM = "_ui_feedback.css"
MODAL = "_modal.css"
ASK = "_watch_ask.css"
WATCH_LIST = "_watch_list.css"
PLAY_ON = "_play_on.css"

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
    assert style(".ui-confirm-overlay.is-visible::before", CONFIRM)["opacity"] == "1"
    assert style(".ui-confirm-overlay.is-visible .ui-confirm-card", CONFIRM)["opacity"] == "1"
    # Both on the overlay's timing, which ui-feedback.js reads as it closes.
    assert style(".ui-confirm-overlay::before", CONFIRM)["transition"] == "inherit"
    assert style(".ui-confirm-card", CONFIRM)["transition"] == "inherit"


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
    assert "modal-sheet-in" in style(".modal-backdrop > .modal-sheet", MODAL)["animation"]


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
def test_on_a_phone_a_dialog_over_glass_is_solid_so_no_third_layer_blurs(
    over: str, card: str, sheet: str
) -> None:
    drawn = style(over, sheet, PHONE)
    assert drawn["background"] == "var(--g-solid)"
    assert drawn["backdrop-filter"] == drawn["-webkit-backdrop-filter"] == "none"


def test_the_download_mark_on_an_answer_s_poster_is_a_solid_chip_that_blurs_nothing() -> None:
    drawn = style(".watch-badge--get", ASK)
    assert drawn["background"] == "var(--g-tip)"
    assert "backdrop-filter" not in drawn and "-webkit-backdrop-filter" not in drawn
    assert contrast((255.0, 255.0, 255.0), _on(token("--g-tip"), WHITE)) >= 4.5
