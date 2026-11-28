"""Glass in the player and on a TV's remote: the controls, menus, the audio and subtitles chooser,
the cards, the remote and its small bar are the one material, every word holds 4.5:1 over a white
frame, glass fades itself and never a parent holding it, and a phone never stacks a third layer."""

from __future__ import annotations

import re

import pytest

from tests.unit.web._css import CSS_DIR, _top_level, contrast, rgb, rules, style, text, token
from tests.unit.web.test_glass_css import BLACK, WHITE, _colour, _glass_over, _over

PLAYER = "_player.css"
RATINGS = "_ratings.css"
REMOTE = "_tv_remote.css"
PHONE = "(max-width: 699.98px)"
FORCED = "(forced-colors: active)"
SHORT_TOUCH = "(max-height: 500px) and (pointer: coarse)"
# What opens over the picture, and the remote's own menu: one rule draws their glass.
OPENED = [".player-menu", ".player-av", ".player-card", ".tv-po"]


def _specificity(selector: str) -> tuple[int, int, int]:
    """(ids, classes, types) of *selector*, an :is(), :not() or :has() counting as its most specific
    argument and a :where() as nothing."""
    ids = classes = types = 0
    i = 0
    while i < len(selector):
        c = selector[i]
        if selector.startswith("::", i):
            types += 1
            i = re.match(r"::[\w-]+", selector[i:]).end() + i
        elif c == ":":
            name = re.match(r":([\w-]+)", selector[i:])[1]
            i += len(name) + 1
            if i < len(selector) and selector[i] == "(":
                depth, j = 1, i + 1
                while depth:
                    depth += {"(": 1, ")": -1}.get(selector[j], 0)
                    j += 1
                if name in ("is", "not", "has"):
                    best = max(
                        _specificity(arg) for arg in _top_level(selector[i + 1 : j - 1], ",")
                    )
                    ids, classes, types = ids + best[0], classes + best[1], types + best[2]
                elif name != "where":
                    classes += 1
                i = j
            else:
                classes += 1
        elif c == "#":
            ids += 1
            i += 1
        elif c in ".[":
            classes += 1
            i = (selector.index("]", i) if c == "[" else i) + 1
        elif c.isalpha():
            types += 1
            i = re.match(r"[\w-]+", selector[i:]).end() + i
        else:
            i += 1
    return ids, classes, types


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
    remote = style(".tv-rem__glass", REMOTE)
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


# What the bars draw: each fades itself at rest, the pills by their glass and their words.
IDLE_PIECES = [
    ".player-back",
    ".player-titles",
    ".player-seek-row",
    ".player-volume",
    ".player-btn",
]


def _idle_faded() -> list[str]:
    """What the rule that takes the bars' pieces to opacity 0 at rest names in its :is()."""
    named = []
    for where, selectors, decls in rules(PLAYER):
        for sel in selectors:
            found = re.fullmatch(r"\.player\.is-idle \.player-chrome :is\((.*)\)", sel)
            if not where and found and decls.get("opacity") == "0":
                named += _top_level(found[1], ",")
    return named


@pytest.mark.parametrize("piece", IDLE_PIECES)
def test_every_piece_of_the_bars_is_out_of_sight_at_rest(piece: str) -> None:
    assert any(re.match(rf"{re.escape(piece)}(?![\w-])", item) for item in _idle_faded()), piece


@pytest.mark.parametrize("piece", IDLE_PIECES)
def test_every_piece_of_the_bars_fades_out_rather_than_blinks(piece: str) -> None:
    fades = [
        decls["transition"]
        for where, selectors, decls in rules(PLAYER)
        if not where
        and "transition" in decls
        and any(re.search(rf"{re.escape(piece)}(?![\w-])", sel) for sel in selectors)
    ]
    assert any("opacity var(--dur-slow)" in t for t in fades), (piece, fades)


def test_a_pill_fades_its_words_and_its_glass_at_rest() -> None:
    assert "color var(--dur-slow)" in style(".player-pill", PLAYER)["transition"]
    assert "opacity var(--dur-slow)" in style(".player-pill::before", PLAYER)["transition"]


def test_in_forced_colours_a_resting_pill_s_words_go_too() -> None:
    # Forced colours draw words in a system colour, so a clear colour no longer hides them; the
    # pill's glass blurs nothing in that mode, so it fades whole.
    assert style(".player.is-idle .player-chrome .player-pill", PLAYER, FORCED)["opacity"] == "0"


@pytest.mark.parametrize("sheet", [PLAYER, REMOTE])
def test_the_prefixed_filter_comes_first_so_the_standard_one_wins(sheet: str) -> None:
    pairs = 0
    for where, selectors, decls in rules(sheet):
        names = list(decls)
        if "backdrop-filter" in decls and "-webkit-backdrop-filter" in decls:
            pairs += 1
            assert names.index("-webkit-backdrop-filter") < names.index("backdrop-filter"), (
                where,
                selectors,
            )
    assert pairs


def test_a_bar_s_shade_fades_with_its_controls() -> None:
    bar = style(".player-chrome", PLAYER)
    assert "var(--player-shade" in bar["--scrim"]
    assert bar["transition"].startswith("--player-shade ")
    assert style(".player.is-idle .player-chrome", PLAYER)["--player-shade"] == "0"
    assert re.search(r"@property --player-shade \{[^}]*inherits: true", text(PLAYER))


def test_a_bar_is_fully_shaded_where_its_shade_is_not_registered() -> None:
    # Without @property (Safari before 16.4) the shade is unset at rest: with no fallback the
    # bars' gradients are dropped and nothing darkens the picture under their words.
    fallback = re.search(
        r"var\(--player-shade, ([^)]+)\)", style(".player-chrome", PLAYER)["--scrim"]
    )
    initial = re.search(r"@property --player-shade \{[^}]*initial-value: ([^;]+);", text(PLAYER))
    assert fallback and initial
    assert fallback[1].strip() == initial[1].strip() == "1"


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


def test_on_the_remote_s_card_a_control_is_a_veil_that_blurs_nothing() -> None:
    # The card fades its controls in as it opens: a blur of their own under a fading parent would
    # blur nothing, and over the card's glass it would be a second layer.
    for control in (
        ".tv-rem .player-back",
        ".tv-rem .player-pill:not(.player-pill--primary)::before",
    ):
        drawn = style(control, REMOTE)
        assert drawn["backdrop-filter"] == drawn["-webkit-backdrop-filter"] == "none", control


@pytest.mark.parametrize(("veil", "sheet"), [(".player-av-veil", PLAYER), (".tv-po-veil", REMOTE)])
def test_a_phone_sheet_dims_the_picture_with_a_plain_veil(veil: str, sheet: str) -> None:
    drawn = style(veil, sheet, PHONE)
    assert drawn["background"] == "var(--g-dim)"
    assert "backdrop-filter" not in drawn and "-webkit-backdrop-filter" not in drawn


@pytest.mark.parametrize("media", [PHONE, SHORT_TOUCH])
def test_on_a_phone_the_bar_over_a_title_is_solid(media: str) -> None:
    # A title's dim and sheet are two layers under the bar already.
    drawn = style("html.has-ttl .tv-mini", REMOTE, media)
    assert drawn["background"] == "var(--g-solid)"
    assert drawn["backdrop-filter"] == drawn["-webkit-backdrop-filter"] == "none"


@pytest.mark.parametrize("media", [PHONE, SHORT_TOUCH])
def test_on_a_phone_a_sheet_over_the_remote_is_opaque_so_no_control_shows_through(
    media: str,
) -> None:
    # Unblurred, any see-through left draws the remote's white controls under the sheet's words.
    drawn = style(".tv-rem :is(.player-av, .player-card, .player-menu, .tv-po)", REMOTE, media)
    assert drawn["background"] == "rgb(var(--g-tint))"
    assert drawn["backdrop-filter"] == drawn["-webkit-backdrop-filter"] == "none"


def test_on_a_phone_the_remote_s_sheet_is_solid_so_the_page_s_glass_stays_under_it() -> None:
    # The sheet leaves the page in sight above it: blurring, it would be a layer over the page's.
    drawn = style(".tv-rem__glass", REMOTE, PHONE)
    assert drawn["background"] == "rgb(var(--g-tint))"
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


@pytest.mark.parametrize(
    ("scope", "sheet"), [(".player", PLAYER), (".tv-rem", REMOTE), (".tv-mini", REMOTE)]
)
def test_a_control_with_no_ring_of_its_own_takes_the_glass_s_ring_not_the_page_s(
    scope: str, sheet: str
) -> None:
    # The page's ring is near the glass's own tone over a white frame (about 2:1), as on the rate
    # card's thumbs and close button, which draw no ring of their own.
    assert style(f"{scope} :focus-visible", sheet)["outline-color"] == "var(--g-focus)"
    ring = rgb(token(token("--g-focus")[4:-1]))
    assert contrast(ring, _glass("--g-a")) >= 3


@pytest.mark.parametrize("sheet", [PLAYER, REMOTE])
def test_no_word_or_tick_is_in_the_blue_kept_for_fills(sheet: str) -> None:
    # The fills' blue is under 4.5:1 on glass over a white frame.
    for where, selectors, decls in rules(sheet):
        assert decls.get("color") != "var(--accent)", (where, selectors)


def test_the_bar_s_progress_holds_3_to_1_on_its_glass_over_a_white_page() -> None:
    fill = _colour(style(".tv-mini__bar i", REMOTE)["background"], {})
    glass = _glass("--g-a")
    assert contrast(_over(fill, glass), glass) >= 3


def test_once_rated_the_other_names_keep_5_to_1_on_the_card_over_a_white_frame() -> None:
    # Clear of the 4.5:1 edge, so a later step of the glass's greys does not take them under it.
    scope = style(".player-card", PLAYER)
    bar = _over(_colour(scope["--s3"], {}), _glass("--g-a"))  # the grid bar's fill: --s3
    words = _colour(style(".player", PLAYER)["--t2"], {})
    # The ratings sheet steps them back on every page; it loads after this one on the player's.
    names = re.compile(
        r"(\.player-rate )?\.rate\.has \.rate__b:not\(\.is-on\) > (\.rate__lab|:is\(.*\.rate__lab.*\))"
    )
    found = [
        (_specificity(sel), order, float(decls["opacity"]))
        for order, sheet in enumerate([PLAYER, RATINGS])
        for where, selectors, decls in rules(sheet)
        if not where and "opacity" in decls
        for sel in selectors
        if names.fullmatch(sel)
    ]
    assert found
    step = max(found)[2]
    seen = _over((*words[:3], words[3] * step), bar)
    assert contrast(seen, bar) >= 5


def test_the_chosen_thumb_s_see_through_wash_has_a_floor_on_the_card() -> None:
    assert style(".player-rate .rate__hl", PLAYER)["box-shadow"] == "var(--g-chip-floor)"


def test_the_bar_the_card_grew_out_of_is_out_of_sight_yet_can_take_the_focus_back() -> None:
    # Hidden by visibility it could not be given the focus as the card closes.
    assert style("html.has-tv-remote .tv-mini", REMOTE)["opacity"] == "0"
    assert not any(
        decls.get("visibility") == "hidden"
        for _, selectors, decls in rules(REMOTE)
        for sel in selectors
        if "has-tv-rem" in sel
    )


def test_closing_the_remote_shows_the_bar_before_it_gives_it_the_focus() -> None:
    # The class that keeps the bar out of sight goes first in close().
    js = (CSS_DIR.parent / "js" / "tv-remote.js").read_text(encoding="utf-8")
    close = js[js.index("function close(") :]
    close = close[: close.index("\n  }\n")]
    shown = close.index("classList.remove('has-tv-remote')")
    assert shown < close.index("ModalA11y.onClosed")
    assert shown < close.index(".focus(")


# What holds a pill's glass among what is faded when it cannot be used.
SPARED = {PLAYER: [".player-menu-wrap"], REMOTE: [".player-av-wrap", ".player-pill"]}


@pytest.mark.parametrize("sheet", [PLAYER, REMOTE])
def test_a_dead_control_fades_a_pill_s_glass_itself_never_a_parent_of_it(sheet: str) -> None:
    # Glass in a parent with opacity below 1 blurs nothing: a dead pill, or a wrap holding one,
    # fades its glass and its words instead.
    faded = 0
    for where, selectors, decls in rules(sheet):
        for sel in selectors:
            dead = ".is-dead" in sel or "[inert]" in sel
            if dead and decls.get("opacity", "1") != "1" and not sel.endswith("::before"):
                faded += 1
                spared = sel[sel.index(":not(") :] if ":not(" in sel else ""
                for holder in SPARED[sheet]:
                    assert holder in spared, (where, sel, holder)
    assert faded
    pill = {
        PLAYER: ".player.is-away .player-menu-wrap[data-away-idle][inert] .player-pill",
        REMOTE: ".tv-rem .is-dead .player-pill",
    }[sheet]
    assert float(style(pill + "::before", sheet)["opacity"]) < 1


@pytest.mark.parametrize(
    ("pill", "sheet"),
    [
        (
            ".tv-rem :is(.player-pill.is-dead, .is-dead .player-pill):not(.player-pill--primary)",
            REMOTE,
        ),
        (".player.is-away .player-menu-wrap[data-away-idle][inert] .player-pill", PLAYER),
    ],
)
def test_a_dead_pill_s_words_fade_with_its_glass(pill: str, sheet: str) -> None:
    assert style(pill, sheet)["color"] == "color-mix(in srgb, var(--t1) 40%, transparent)"


@pytest.mark.parametrize(
    "glass",
    [
        ".tv-rem .player-pill.is-dead:not(.player-pill--primary)::before",
        ".tv-rem .is-dead .player-pill::before",
    ],
)
def test_a_dead_pill_s_glass_fades_whether_it_or_its_wrap_is_dead(glass: str) -> None:
    assert float(style(glass, REMOTE)["opacity"]) < 1


def test_a_dead_stop_fades_its_red_rather_than_turning_white() -> None:
    dead = ".tv-rem :is(.player-pill.is-dead, .is-dead .player-pill):not(.player-pill--primary)"
    stop = next(
        sel
        for _, selectors, decls in rules(REMOTE)
        if "var(--danger-text)" in decls.get("color", "")
        for sel in selectors
        if "tv-rem__danger" in sel and ".is-dead" in sel
    )
    assert _specificity(stop) > _specificity(dead)
    assert "player-pill" in stop  # the generic rule's subject, so both meet the same element


@pytest.mark.parametrize(
    ("disc", "sheet", "media"),
    [
        (".tv-rem [data-play]", REMOTE, None),
        (".player.is-away [data-play]", PLAYER, "(max-width: 899.98px)"),
    ],
)
def test_the_ring_of_a_white_disc_lies_outside_it_on_the_glass(
    disc: str, sheet: str, media: str | None
) -> None:
    # Inside, the light ring on white is about 1.6:1.
    assert style(disc, sheet, media)["background"] == "var(--t1)"
    offset = style(disc + ":focus-visible", sheet, media)["outline-offset"]
    assert float(offset.removesuffix("px")) > 0
    assert _specificity(disc + ":focus-visible") > _specificity(".player-btn:focus-visible")


@pytest.mark.parametrize("media", [PHONE, SHORT_TOUCH])
def test_over_a_title_the_remote_and_the_title_s_sheet_are_the_only_layers(media: str) -> None:
    # The title's dim lies under its sheet, beside the title, and stays drawn under the remote.
    assert "backdrop-filter: var(--g-dim-blur)" in text("_watch_detail.css")
    assert style("html.has-tv-rem", REMOTE, media)["--g-dim-blur"] == "none"


def test_words_on_the_remote_s_picture_keep_4_5_to_1_over_a_white_frame() -> None:
    # The title and episode lie at the foot of the picture, on the darkest of its shade.
    foot = _top_level(style(".tv-rem__shade", REMOTE)["background"], ",")[-1]
    shade = rgb(re.search(r"rgb\([^)]*\)", foot)[0])
    assert shade[:3] == (0, 0, 0)
    behind = tuple(255 * (1 - shade[3]) for _ in range(3))
    assert contrast(WHITE, behind) >= 4.5
