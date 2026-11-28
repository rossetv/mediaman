"""Glass in the player and on a TV's remote: the controls, menus, the audio and subtitles chooser,
the cards, the remote and its small bar are the one material, every word holds 4.5:1 over a white
frame, glass fades itself and never a parent holding it, and a phone never stacks a third layer."""

from __future__ import annotations

import re

import pytest

from tests.unit.web._css import contrast, rules, style, text, token
from tests.unit.web.test_glass_css import BLACK, WHITE, _colour, _glass_over, _over

PLAYER = "_player.css"
REMOTE = "_tv_remote.css"
PHONE = "(max-width: 699.98px)"
SHORT_TOUCH = "(max-height: 500px) and (pointer: coarse)"
# What opens over the picture, and the remote's own menu: one rule draws their glass.
OPENED = [".player-menu", ".player-av", ".player-card", ".tv-po"]


def _glass(alpha: str) -> tuple[float, ...]:
    """Glass of the tint opacity *alpha* (a token's name) over a white frame."""
    return _glass_over(WHITE, float(token(alpha)), filtered=True)


@pytest.mark.parametrize("surface", OPENED)
def test_what_opens_over_the_picture_is_glass_with_the_same_filter_in_every_engine(
    surface: str,
) -> None:
    drawn = style(surface, PLAYER)
    assert drawn["background"] == "var(--g-regular)"
    assert drawn["backdrop-filter"] == drawn["-webkit-backdrop-filter"] == "var(--g-filter)"
    assert drawn["border-top-color"] == "var(--g-lit)"


@pytest.mark.parametrize("control", [".player-back", ".player-pill::before", ".player-lost-text"])
def test_a_control_s_glass_halves_the_picture_in_every_engine(control: str) -> None:
    drawn = style(control, PLAYER)
    assert drawn["background"] == "var(--player-glass)"
    assert drawn["backdrop-filter"] == drawn["-webkit-backdrop-filter"] == "var(--g-filter)"


@pytest.mark.parametrize(("root", "sheet"), [(".player", PLAYER), (".tv-rem", REMOTE)])
def test_the_player_and_the_remote_draw_their_controls_in_the_player_s_glass(
    root: str, sheet: str
) -> None:
    drawn = style(root, sheet)
    assert drawn["--player-glass"] == "var(--g-player)"
    assert drawn["--player-glass-hover"] == "var(--g-player-hover)"


def test_the_remote_is_thick_glass_and_its_bar_thin() -> None:
    remote = style(".tv-rem", REMOTE)
    assert remote["background"] == "var(--g-thick)"
    assert remote["backdrop-filter"] == remote["-webkit-backdrop-filter"] == "var(--g-filter)"
    bar = style(".tv-mini", REMOTE)
    assert bar["background"] == "var(--g-thin)"
    assert bar["backdrop-filter"] == bar["-webkit-backdrop-filter"] == "var(--g-filter)"
    assert bar["border-top-color"] == "var(--g-lit)"


# Each scope that sets the words on its glass, the sheet, and the thinnest tint they lie on.
SCOPES = [
    (".player", PLAYER, "--g-a-player"),
    (".tv-rem", REMOTE, "--g-a-player"),
    (".player-menu", PLAYER, "--g-a"),
    (".tv-mini", REMOTE, "--g-a"),
]


@pytest.mark.parametrize(("scope", "sheet", "alpha"), SCOPES)
def test_every_quiet_blue_and_red_word_on_this_glass_keeps_4_5_to_1_over_a_white_frame(
    scope: str, sheet: str, alpha: str
) -> None:
    declared = style(scope, sheet)
    glass = _glass(alpha)
    for name in ("--t2", "--t3", "--t4", "--accent-hi", "--accent-text", "--danger-text"):
        assert name in declared, f"{scope} leaves {name} at its colour for solid grey"
        words = _over(_colour(declared[name], {}), glass)
        assert contrast(words, glass) >= 4.5, (scope, name)


# A control's words, and the fill under them in each state: a pill's own glass over the halved
# frame (None), or a fill lying on glass of the tint opacity named.
STATES = [
    # The keep pill and Stop, red on the controls' glass, at rest, lit and pressed.
    (".player-keep-btn", ".player-pill::before", None),
    (".player-keep-btn", ".player-pill:hover::before", None),
    (".player-away-stop", ".player-pill:active::before", None),
    # A quiet pill on a card's glass, at rest and lit.
    (None, ".player-card .player-pill:not(.player-pill--primary)::before", "--g-a"),
    (
        None,
        ".player-card .player-pill:not(.player-pill--primary):is(:hover, :active)::before",
        "--g-a",
    ),
    # The chooser's second line and a menu's detail, on a row lit by a key or a press.
    (".player-av-sub", ".player-av-row:focus-visible", "--g-a"),
    (".player-av-sub", ".player-av-row:active", "--g-a"),
    (".player-menu-side", ".player-menu-item:active", "--g-a"),
]


@pytest.mark.parametrize(("words", "fill", "alpha"), STATES)
def test_a_control_s_words_keep_4_5_to_1_over_a_white_frame_in_each_state(
    words: str | None, fill: str, alpha: str | None
) -> None:
    scope = {
        **style(".player", PLAYER),
        **style(".player-menu", PLAYER),
        "--g-player": token("--g-player"),
        "--g-player-hover": token("--g-player-hover"),
    }
    under = _glass_over(WHITE, 0.0, filtered=True) if alpha is None else _glass(alpha)
    lit = _over(_colour(style(fill, PLAYER)["background"], scope), under)
    colour = "#fff" if words is None else style(words, PLAYER)["color"]
    seen = _over(_colour(colour, scope), lit)
    assert contrast(seen, lit) >= 4.5, (words, fill, round(contrast(seen, lit), 2))


def test_a_quiet_pill_on_a_card_stands_out_from_the_card_over_white_and_black() -> None:
    pill = style(".player-card .player-pill:not(.player-pill--primary)::before", PLAYER)
    for frame in (WHITE, BLACK):
        card = _glass_over(frame, float(token("--g-a")), filtered=True)
        veil = _over(_colour(pill["background"], style(".player-menu", PLAYER)), card)
        assert contrast(veil, card) >= 1.4, frame
    # It lies on the card's glass: a blur of its own would be one more layer.
    assert pill["backdrop-filter"] == pill["-webkit-backdrop-filter"] == "none"


def test_a_white_pill_blurs_nothing_as_nothing_shows_through_it() -> None:
    drawn = style(".player-pill--primary::before", PLAYER)
    assert drawn["background"] == "var(--t1)"
    assert drawn["backdrop-filter"] == drawn["-webkit-backdrop-filter"] == "none"


def test_the_seek_bar_s_label_is_solid_over_the_picture() -> None:
    label = style(".player-bubble-text", PLAYER)
    assert label["background"] == "var(--g-tip)"
    assert label["box-shadow"].startswith("0 0 0 1px var(--g-edge)")


def test_resting_controls_fade_their_glass_and_words_and_never_a_bar_holding_glass() -> None:
    # Glass under a parent with opacity below 1 blurs none of the picture while it fades.
    for where, selectors, decls in rules(PLAYER):
        if any(".player-chrome" in sel and "is-idle" in sel for sel in selectors):
            for sel in selectors:
                if sel.endswith(".player-chrome"):
                    assert "opacity" not in decls, (where, sel)
    assert style(".player.is-idle .player-chrome .player-pill::before", PLAYER)["opacity"] == "0"
    assert style(".player.is-idle .player-chrome .player-pill", PLAYER)["color"] == "transparent"
    assert "opacity" not in style(".player-chrome", PLAYER)


def test_a_bar_s_shade_fades_with_its_controls() -> None:
    bar = style(".player-chrome", PLAYER)
    assert "var(--player-shade)" in bar["--scrim"]
    assert bar["transition"].startswith("--player-shade ")
    assert style(".player.is-idle .player-chrome", PLAYER)["--player-shade"] == "0"
    assert re.search(r"@property --player-shade \{[^}]*inherits: true", text(PLAYER))


@pytest.mark.parametrize(
    "hidden",
    [
        ".player:not(.is-away):has(.player-bubble:not([hidden], .is-leaving), .player-upnext:not([hidden], .is-leaving), .player-rate:not([hidden], .is-leaving, .player-rate--beside)) .player-chapters",
        ".player:not(.is-away):has(.player-rate--beside:not([hidden], .is-leaving)) .player-chapters",
    ],
)
def test_the_chapters_pill_goes_by_its_own_glass_and_words(hidden: str) -> None:
    for where, selectors, decls in rules(PLAYER):
        if hidden in selectors:
            assert "opacity" not in decls, where
    phone = "(max-width: 600px)" if "player-bubble" in hidden else None
    media = phone or next(w for w, sels, _ in rules(PLAYER) if hidden in sels)
    assert style(hidden + " .player-pill::before", PLAYER, media)["opacity"] == "0"


def test_the_remote_s_row_of_controls_rises_without_a_fade_of_its_own() -> None:
    name = style(".tv-rem .player-bottom", REMOTE)["animation"].split()[0]
    frames = re.search(rf"@keyframes {name} \{{(.*?)\}}\n", text(REMOTE))[1]
    assert "opacity" not in frames


@pytest.mark.parametrize(("veil", "sheet"), [(".player-av-veil", PLAYER), (".tv-po-veil", REMOTE)])
def test_a_phone_sheet_dims_the_picture_with_a_plain_veil(veil: str, sheet: str) -> None:
    drawn = style(veil, sheet, PHONE)
    assert drawn["background"] == "var(--g-dim)"
    assert "backdrop-filter" not in drawn and "-webkit-backdrop-filter" not in drawn


@pytest.mark.parametrize("media", [PHONE, SHORT_TOUCH])
@pytest.mark.parametrize(
    "over",
    [
        ".tv-rem :is(.player-av, .player-card, .player-menu, .tv-po)",
        "html.has-ttl .tv-mini",
    ],
)
def test_on_a_phone_what_would_be_a_third_layer_of_glass_is_solid(over: str, media: str) -> None:
    # The remote and its pills are two layers already; a title's dim and sheet are two under the bar.
    drawn = style(over, REMOTE, media)
    assert drawn["background"] == "var(--g-solid)"
    assert drawn["backdrop-filter"] == drawn["-webkit-backdrop-filter"] == "none"


def test_the_ask_card_dims_the_picture_plainly() -> None:
    dim = style(".player-ask::backdrop", PLAYER)
    assert dim["background"] == "var(--g-dim)"
    assert "backdrop-filter" not in dim


@pytest.mark.parametrize("sheet", [PLAYER, REMOTE])
def test_every_keyboard_ring_in_the_player_and_the_remote_is_the_glass_s_blue(sheet: str) -> None:
    rings = 0
    for where, selectors, decls in rules(sheet):
        for prop in ("outline", "outline-color"):
            if prop in decls and "var(--" in decls[prop]:
                rings += 1
                assert "var(--g-focus)" in decls[prop], (where, selectors)
    assert rings


@pytest.mark.parametrize("sheet", [PLAYER, REMOTE])
def test_no_word_or_tick_is_in_the_blue_kept_for_fills(sheet: str) -> None:
    # The fills' blue is under 4.5:1 on glass over a white frame.
    for where, selectors, decls in rules(sheet):
        assert decls.get("color") != "var(--accent)", (where, selectors)


def test_the_bar_s_progress_holds_3_to_1_on_its_glass_over_a_white_page() -> None:
    fill = _colour(style(".tv-mini__bar i", REMOTE)["background"], {})
    glass = _glass("--g-a")
    assert contrast(_over(fill, glass), glass) >= 3


def test_once_rated_the_other_names_keep_4_5_to_1_on_the_card_over_a_white_frame() -> None:
    scope = style(".player-card", PLAYER)
    bar = _over(_colour(scope["--s3"], {}), _glass("--g-a"))  # the grid bar's fill: --s3
    words = _colour(style(".player", PLAYER)["--t2"], {})
    step = float(
        style(".player-rate .rate.has .rate__b:not(.is-on) > .rate__lab", PLAYER)["opacity"]
    )
    seen = _over((*words[:3], words[3] * step), bar)
    assert contrast(seen, bar) >= 4.5


def test_the_chosen_thumb_s_see_through_wash_has_a_floor_on_the_card() -> None:
    assert style(".player-rate .rate__hl", PLAYER)["box-shadow"] == "var(--g-chip-floor)"


@pytest.mark.parametrize("media", [PHONE, SHORT_TOUCH])
def test_on_a_phone_the_page_s_glass_is_out_of_sight_under_the_remote(media: str) -> None:
    # The bar's islands, the small bar, a title, a panel or the search's sheet under the remote's glass and its
    # buttons' would be a third layer and more.
    hidden = style(
        "html.has-tv-rem :is(.nav-glass, .tv-mini, .ttl, .space, .you, .watch-sheet)", REMOTE, media
    )
    assert hidden["visibility"] == "hidden"
