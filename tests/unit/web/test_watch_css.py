"""What the Watch pages' stylesheet has to hold for its badges and toggles to be seen, read as rules."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.unit.web._css import px, rules, style, token

SHEET = Path(__file__).resolve().parents[3] / "src/mediaman/web/static/css/_watch.css"
FORCED = "(forced-colors: active)"


def _of(selector: str, media: str | None = None, sheet: Path = SHEET) -> str:
    """The declarations of *selector* outside any `@media`, or inside those asking for *media*."""
    css = re.sub(r"/\*.*?\*/", "", sheet.read_text(encoding="utf-8"), flags=re.S)
    blocks = r"@(?:media|container) ([^{]+)\{((?:[^{}]*\{[^{}]*\})*)\s*\}"
    text = (
        "".join(body for query, body in re.findall(blocks, css) if media in query)
        if media
        else re.sub(blocks, "", css)
    )
    return "".join(
        body
        for head, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text)
        if selector in [s.strip() for s in re.split(r",(?![^()]*\))", head)]
    )


def test_forced_colours_paint_a_watched_toggle_s_ring_and_not_all_it_takes_to_press() -> None:
    assert "background: Highlight" in _of('.watch-mark[aria-pressed="true"]::before', FORCED)
    assert "background" not in _of('.watch-mark[aria-pressed="true"]', FORCED)
    assert "background: Highlight" in _of('.watch-mark-btn[aria-pressed="true"]', FORCED)


def _background(selector: str) -> str:
    return re.split(r"--badge-bg:|background:", _of(selector), maxsplit=1)[1].split(";")[0].strip()


def test_a_leaving_badge_is_dark_and_turns_red_only_when_it_leaves_now() -> None:
    dark = re.fullmatch(
        r"color-mix\(in srgb, var\(--scrim\) (\d+)%, transparent\)",
        _background(".watch-badge--leaving"),
    )
    # White on black this dense over a white poster is 4.5:1 from 55%.
    assert dark and int(dark.group(1)) >= 55
    red = _background(".watch-badge--leaving-now")
    assert "var(--danger)" in red and "transparent" not in red
    css = SHEET.read_text(encoding="utf-8")
    assert css.index(".watch-badge--leaving-now {") > css.index(".watch-badge--leaving {")


@pytest.mark.parametrize(
    "seen",
    [
        ".watch-badge--watched",
        ".watch-badge--unwatched",
        ".watch-pill__mark",
        '.watch-mark[aria-pressed="true"]',
        ".watch-mark-btn__on",
    ],
)
def test_the_eye_of_what_is_watched_is_another_one(seen: str) -> None:
    assert _of(seen).count("--eye-open: none; --eye-seen: inline;") == 1
    assert _of(".eye-open").strip() == "display: var(--eye-open, inline);"
    assert _of(".eye-seen").strip() == "display: var(--eye-seen, none);"
    sprite = (SHEET.parents[2] / "templates/_icons.html").read_text(encoding="utf-8")
    eye = re.search(r'<symbol id="i-eye".*?</symbol>', sprite)
    assert eye and 'class="eye-open"' in eye[0] and 'class="eye-seen" display="none"' in eye[0]


def test_a_watched_toggle_is_filled_and_outshines_no_title() -> None:
    fill = re.search(r"var\(--t1\) (\d+)%", _background('.watch-mark[aria-pressed="true"]::before'))
    # Dark text needs 50% of the fill for its contrast; much over 60% the column of pills glares.
    assert fill and 55 <= int(fill[1]) <= 65
    assert "color: var(--bg)" in _of('.watch-mark[aria-pressed="true"]')
    assert "background" not in _of(".watch-mark::before").replace("background-color", "")


FORCED_CHOICES = [
    (SHEET, ".watch-pill.on"),
    (SHEET.with_name("_buttons.css"), ".fpill.on"),
    (SHEET.with_name("_dl.css"), ".dl-bar-fill"),
    (SHEET.with_name("_dl.css"), ".dl-hero-fill"),
    (SHEET.with_name("_dl.css"), ".dl-row-bar-fill"),
    (SHEET.with_name("_dl.css"), ".dl-compact-fill"),
    (SHEET.with_name("_dl.css"), ".dl-ep-mini-fill"),
]


@pytest.mark.parametrize(("sheet", "selector"), FORCED_CHOICES)
def test_forced_colours_show_what_is_on_and_how_far_a_download_is(
    sheet: Path, selector: str
) -> None:
    assert "background: Highlight" in _of(selector, FORCED, sheet)


def test_forced_colours_give_keep_an_edge() -> None:
    assert "border: 1px solid" in _of(".watch-keep::before", FORCED)


def test_forced_colours_give_a_hero_whose_art_they_hide_no_room_kept_for_the_art() -> None:
    heroes = [
        ("_dl.css", (".dl-hero-bg", ".dl-hero-poster"), {".dl-hero": "0", ".dl-hero-content": "0"}),
        (
            "_watch.css",
            (".watch-banner__bg",),
            {".watch-banner:has(> .watch-banner__bg--art:not([data-broken]))": "0"},
        ),
        (
            "_watch_detail.css",
            (".ttl__art", ".ttl__fill"),
            # As tall as the hero of a title with no art to show.
            {
                ".ttl:not(.is-trailer) .ttl__hero": style(
                    ".ttl.is-short:not(.is-trailer) .ttl__hero", "_watch_detail.css"
                )["min-height"]
            },
        ),
    ]
    for sheet, art, rooms in heroes:
        for hidden in art:
            assert style(hidden, sheet, FORCED)["display"] == "none", hidden
        for hero, room in rooms.items():
            assert style(hero, sheet, FORCED)["min-height"] == room, hero


def test_a_leaving_badge_has_an_edge_in_forced_colours() -> None:
    # Round its words, inside the window that would cut it off, and not over the poster's width.
    for pill in (".watch-badge__short", ".watch-badge__tiny::after", ".watch-badge__least::after"):
        drawn = _of(pill, FORCED)
        assert "outline: 1px solid CanvasText" in drawn and "outline-offset: -1px" in drawn
        assert "background: Canvas" in drawn
    assert not _of(".watch-badge--leaving", FORCED)


def test_a_watched_badge_is_its_icon_alone_on_the_narrowest_phones_and_still_says_the_word() -> (
    None
):
    word = _of(".watch-badge__word", "max-width: 359.98px")
    assert "clip-path: inset(50%)" in word
    # Out of the reading, the badge would say nothing.
    assert "display: none" not in word


CARDS = SHEET.with_name("_watch_cards.css")


def test_a_caption_ends_before_the_thumb_of_any_card_that_has_one() -> None:
    rules = _of(".watch-card.has-tools .watch-card__sub", sheet=CARDS)
    pad = re.search(r"padding-right: (\d+)px", rules)
    # The widest thumb is 21px, in the middle of 30 that end 5px past the card.
    assert pad and int(pad.group(1)) >= (30 + 21) / 2 - 5


def _compounds(css: str) -> list[str]:
    """The compound selectors of *css*, each with what is in its own parentheses."""
    heads = re.findall(r"([^{};]+)\{", re.sub(r"/\*.*?\*/", "", css, flags=re.S))
    found, depth, one = [], 0, ""
    for char in ",".join(h for h in heads if not h.strip().startswith("@")):
        depth += (char == "(") - (char == ")")
        if depth or char not in " \n>~+,":
            one += char
        elif one:
            found.append(one)
            one = ""
    return [*found, one]


CARD = r"\.(?:watch-card|card-tools?|watch-card__(?:poster|link))(?![\w-])"


def _asks_a_card(compound: str) -> bool:
    outside = compound
    while "(" in outside:
        outside = re.sub(r"\([^()]*\)", "", outside)
    return ":has(" in compound and bool(re.search(CARD, outside))


def test_no_rule_asks_by_has_what_a_card_holds() -> None:
    # A browser asks it of every card of the page for each one added, marked or pointed at.
    css = "".join(sheet.read_text(encoding="utf-8") for sheet in SHEET.parent.glob("*.css"))
    found = {one.removesuffix("::after") for one in _compounds(css) if _asks_a_card(one)}
    # These two cost next to nothing: a poster's own child, of which there is one at most.
    assert found <= {
        ".watch-card__poster:has(> .watch-bar)",
        ".watch-card__poster:has(> .card-barend)",
    }


@pytest.mark.parametrize(
    "selector",
    [
        ".watch-card:has(> .card-tools) .watch-card__sub",
        ".watch-card:is(:hover, :has(:focus-visible), .is-lifted) .card-tool",
        ".card-tools:not(:has(> .card-tool--thumb:not([hidden]))) > .card-tool",
        ".watch-card.has-tools:not(:has(.is-previewed)) .watch-card__sub",
    ],
)
def test_a_rule_that_does_would_be_found(selector: str) -> None:
    assert any(_asks_a_card(one) for one in _compounds(selector + " {}"))


def test_every_button_under_a_card_takes_44px_on_a_touch_screen() -> None:
    css = CARDS.read_text(encoding="utf-8")
    size = re.search(r"\.card-tool \{[^}]*?width: (\d+)px;\s*height: (\d+)px", css)
    grown = re.search(r"\.card-tool::after \{[^}]*inset: -(\d+)px -(\d+)px -(\d+)px -(\d+)px", css)
    assert size and grown
    top, right, bottom, left = map(int, grown.groups())
    assert int(size.group(1)) + left + right >= 44 and int(size.group(2)) + top + bottom >= 44


def test_no_button_under_a_card_moves_where_motion_is_unwanted() -> None:
    css = CARDS.read_text(encoding="utf-8")
    still = css.split("@media (prefers-reduced-motion: no-preference)")
    assert "animation:" not in still[0]
    assert "animation:" not in still[1].split("@media (prefers-reduced-motion: reduce)")[1]


FINGER = "(hover: none), (pointer: coarse)"
STILL = "(prefers-reduced-motion: reduce)"
RATINGS = SHEET.with_name("_ratings.css")
PREVIEW = SHEET.with_name("_watch_preview.css")


def _z(selector: str, media: str | None = None) -> int:
    return int(re.findall(r"z-index: (\d+)", _of(selector, media, CARDS))[0])


def test_on_a_touch_screen_the_open_levels_stand_over_every_other_card_s_buttons() -> None:
    lifted = _z(".watch-card.is-lifted", FINGER)
    assert lifted > max(_z(".card-tools"), _z(".card-over"))
    # And under nothing of the row's heading, which reaches down over the posters.
    heading = ".watch-row.has-lifted > .watch-row__head :is(.row-open, .space-row-link)::after"
    assert "pointer-events: none" in _of(heading, FINGER, CARDS)


def test_every_card_button_draws_its_keyboard_ring_on_its_round_backing() -> None:
    assert "outline: none" in _of(".card-tool:focus-visible", sheet=CARDS)
    assert "outline: 2px solid" in _of(".card-tool:focus-visible::before", sheet=CARDS)
    shown = '.card-tool:is(:focus-visible, [aria-expanded="true"])::before'
    assert "opacity: 1" in _of(shown, sheet=CARDS)


MOUSE = "(hover: hover) and (pointer: fine)"


_TOOLS = " > :where(.watch-card__link:not(.is-previewed)) ~ :where(.card-tools) > .card-tool"


def test_with_a_mouse_a_card_a_finger_pressed_shows_its_buttons_as_under_the_mouse() -> None:
    shown = ".watch-card:is(.has-focus, .is-lifted, .is-touched)" + _TOOLS
    assert "opacity: 1; transform: none" in _of(shown, MOUSE, CARDS)
    ring = ".watch-card:is(.has-focus, .is-lifted, .is-touched) .card-tool::before"
    assert "opacity: 1" in _of(ring, MOUSE, CARDS)


def test_with_a_mouse_a_card_under_it_shows_its_buttons_unless_a_finger_was_last() -> None:
    # A finger leaves its card under :hover until it presses elsewhere, so no hover of a card
    # may light its buttons where a finger can be what hovers.
    card_hover = re.compile(r"\.watch-card[\w.-]*(?::is\([^)]*)?:hover")
    hovered = [
        (where, sel, decls)
        for where, sels, decls in rules("_watch_cards.css")
        for sel in sels
        if card_hover.search(sel)
    ]
    assert {where for where, _, _ in hovered} == {(MOUSE,)}
    shown = [
        sel
        for _, sel, decls in hovered
        if sel.endswith(".card-tool") and decls.get("opacity") == "1"
    ]
    assert shown
    assert all(sel.startswith(":where(html:not(.is-by-touch))") for sel in shown), shown


def test_the_levels_can_take_the_focus_as_they_open_where_motion_is_unwanted() -> None:
    assert "transition: none !important" in _of(".rate--lift .rate__set", STILL, CARDS)


def test_a_level_s_name_stays_in_its_card_beside_the_levels() -> None:
    name = _of(".rate__name > span", sheet=CARDS)
    assert "nowrap" not in name and "max-width: 100%" in name
    room = int(re.findall(r"width: calc\(100cqi - (\d+)px\)", _of(".rate__name", sheet=CARDS))[0])
    levels = _of(".rate--lift", sheet=CARDS)
    taken = sum(int(re.findall(rf"{side}: (\d+)px", levels)[0]) for side in ("right", " width"))
    gap = int(re.findall(r"right: calc\(100% \+ (\d+)px\)", _of(".rate__name", sheet=CARDS))[0])
    assert room >= taken + gap
    # On a poster as narrow as a phone's three in a line, no word of a name fits.
    css = CARDS.read_text(encoding="utf-8")
    assert re.search(
        r"@container \(max-width: 104px\) \{\s*\.card-over \.rate__name \{ display: none", css
    )
    # And by a finger not over the card above, whose caption the levels of a short poster reach.
    assert "min(0px, var(--top))" in _of(".card-over .rate__name", FINGER, CARDS)
    badge = _of(".is-lifted > .watch-card__link .watch-badge", FINGER, CARDS)
    assert "opacity: 0" in badge


@pytest.mark.parametrize("level", [".is-up.is-double", ".is-down.is-double"])
def test_a_thumb_keeps_the_colour_of_its_level_from_the_press_to_its_rest(level: str) -> None:
    pressed = re.findall(r"--level: ([^;]+);", _of(f".rate__b{level}", sheet=RATINGS))
    rest = _of(f"{level}:is(.card-tool, .card-flier)", sheet=CARDS)
    assert pressed and re.findall(r"--rate-c: ([^;]+);", rest) == pressed
    preview = _of(f".pv__round{level}", sheet=PREVIEW)
    assert re.findall(r"color: ([^;]+);", preview) == pressed


def test_every_custom_property_a_thumb_is_given_is_read_somewhere() -> None:
    css = "".join(p.read_text(encoding="utf-8") for p in SHEET.parent.glob("*.css"))
    given = {
        name
        for head, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
        if ".rate-th" in head
        for name in re.findall(r"(--[\w-]+):", body)
    }
    assert {name for name in given if f"var({name}" not in css} == set()


def _luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def test_a_level_s_name_is_painted_as_the_thumbs_tips_and_reads_on_its_fill() -> None:
    name, tip = _of(".rate__name > span", sheet=CARDS), _of(".rate__tip", sheet=RATINGS)
    paint = r"(?<![-\w])(background|box-shadow|color): ([^;]+);"
    assert dict(re.findall(paint, name)) == dict(re.findall(paint, tip))
    fill = re.search(r"background: #([0-9a-f]{6});", tip)
    words = re.search(r"(?<![-\w])color: #([0-9a-f]{3}|[0-9a-f]{6});", tip)
    assert fill and words, tip
    hexes = ["#" + (h if len(h) == 6 else "".join(c * 2 for c in h)) for h in (fill[1], words[1])]
    light, dark = sorted(map(_luminance, hexes), reverse=True)
    assert (light + 0.05) / (dark + 0.05) >= 4.5


# "removed", the widest word of a level's name, is 3.97em in the system font at its boldest.
WIDEST_WORD_EM = 3.97


def test_a_level_s_name_breaks_between_words_alone_and_goes_where_one_does_not_fit() -> None:
    css = re.sub(r"/\*.*?\*/", "", CARDS.read_text(encoding="utf-8"), flags=re.S)
    words = "".join(re.findall(r"\.rate__name > span \{([^{}]*)\}", css))
    assert "overflow-wrap: normal" in words
    assert not re.search(r"anywhere|break-word|break-all|hyphens: auto", words)
    # Its room is measured in its own size, so a name too big for it goes as the text grows.
    assert "container-type: inline-size" in _of(".rate__name", sheet=CARDS)
    assert "font-size" not in words
    sized = [_of(".rate__name", sheet=CARDS), _of(".card-over .rate__name", FINGER, CARDS)]
    assert all("font-size" in rule for rule in sized)
    gone = re.search(
        r"@container \(width < ([\d.]+)em\) \{\s*\.rate__name > span \{ visibility: hidden", css
    )
    assert gone and float(gone.group(1)) >= WIDEST_WORD_EM


def test_forced_colours_keep_the_level_given_readable_under_the_pointer() -> None:
    assert "color: HighlightText" in _of(".rate--lift .rate__b.is-on", FORCED, CARDS)
    hover = ".watch-card .card-tool--thumb:is(.is-up, .is-down):hover"
    assert "color: Highlight" in _of(hover, FORCED, CARDS)
    assert "padding-inline: 5px" in _of(".card-over .rate__name", FORCED, CARDS)


def test_the_levels_are_readable_where_the_picture_under_them_is_not_blurred() -> None:
    ground = re.findall(r"var\(--s2\) (\d+)%", _of(".rate--lift .rate__set", sheet=CARDS))
    assert int(ground[0]) >= 90


TOUCH = "(max-width: 599.98px), (pointer: coarse)"
NARROW = "(max-width: 359.98px)"
HEADING_LINE = 18  # the smallest a heading's line is, in WebKit
LINK_LINE = 18  # of 13px text


def _reach(selector: str, media: str) -> tuple[int, int]:
    """How far above and below its line it takes a tap."""
    inset = re.findall(r"inset: ([^;]+);", _of(selector, media))[0].split()
    return -int(inset[0].removesuffix("px")), -int(inset[-1].removesuffix("px"))


@pytest.mark.parametrize(
    ("link", "line"),
    [
        (".watch-row__title > .row-open[href]", HEADING_LINE),
        (".watch-row__head > .space-row-link", LINK_LINE),
    ],
)
def test_a_rows_heading_and_its_link_take_44px_to_tap_over_the_posters_of_their_row(
    link: str, line: int
) -> None:
    assert sum(_reach(f"{link}::after", TOUCH)) + line >= 44
    # Positioned later, the posters' own box would take the lower part.
    assert "z-index: 1" in _of(link, TOUCH)


def test_a_heading_and_the_link_under_it_take_44px_each_on_the_narrowest_phones() -> None:
    head = ".watch-row__head:has(> .space-row-link)"
    heading = _reach(f"{head} > .watch-row__title > .row-open[href]::after", NARROW)
    link = _reach(".watch-row__head > .space-row-link::after", NARROW)
    assert sum(heading) + HEADING_LINE >= 44
    assert sum(link) + LINK_LINE >= 44
    # Neither over the other: 2px are between the two lines.
    assert heading[1] + link[0] <= 2
    # And little of the posters.
    room = int(re.findall(r"margin-bottom: (\d+)px", _of(head, NARROW))[0])
    assert link[1] - room <= 8


def test_a_rows_heading_leaves_the_more_of_the_row_above_its_44px() -> None:
    # As low as a card's More comes over the gap to the next heading.
    assert _reach(".watch-row__title > .row-open[href]::after", TOUCH)[0] <= 9


def test_the_words_of_see_all_keep_their_colour_under_the_pointer_in_forced_colours() -> None:
    browse = SHEET.with_name("_watch_browse.css")
    for state in ("", ":hover", ":active"):
        rules = _of(f".row-open[href]{state} .row-open__cue", FORCED, browse)
        assert "color: LinkText" in rules


def test_the_badges_under_a_leaving_one_stand_in_line_with_it() -> None:
    browse = SHEET.with_name("_watch_browse.css")
    under = ".watch-card__poster > .watch-badge--leaving ~ .watch-badge:not(.watch-badge--leaving)"
    for media in (None, "(max-width: 599.98px)"):
        left = re.findall(r"left: \d+px", _of(".watch-badge--leaving", media))
        assert left and re.findall(r"left: \d+px", _of(under, media, browse)) == left
    assert "right: auto" in _of(under, sheet=browse)


def test_a_category_tile_s_drawing_takes_the_system_colour_in_forced_colours() -> None:
    search = SHEET.with_name("_watch_search.css")
    # The tile's own rule for the drawing's grey is more specific than a bare `.watch-line` one.
    for selector in (
        ".watch-tile > .watch-line",
        ".watch-tile:hover > .watch-line",
        ".watch-tile.is-picked > .watch-line",
    ):
        rules = _of(selector, FORCED, search)
        assert "color: CanvasText" in rules and "forced-color-adjust: auto" in rules


BAR = SHEET.with_name("_watch_search.css")
CONTROLS = ":is(a, button, input, select, textarea, [tabindex])"


def test_a_focus_in_what_holds_still_over_the_page_gets_no_room_that_would_scroll_it() -> None:
    page = 'html[data-page="watch"]'
    assert "scroll-padding-top: var(--bar-clear)" in _of(page, sheet=BAR)
    # While the focus is in the bar or a grid's head, the page has no padding to scroll for.
    held = 'html[data-page="watch"].is-bar-focus'
    assert "scroll-padding-top: 0" in _of(held, sheet=BAR)
    sticky = {
        head.strip()
        for sheet in SHEET.parent.glob("_watch*.css")
        for head, body in re.findall(r"([^{}]+)\{([^{}]*)\}", sheet.read_text(encoding="utf-8"))
        if "position: sticky" in body
    }
    # A title's overlay scrolls itself, under its own padding for its bar.
    assert sticky == {".watch-all__head", ".dlg__head", ".ttl__bar"}


def test_the_skip_link_lands_clear_of_the_bar_at_every_width() -> None:
    # The page's padding and this margin come to the bar's height.
    assert "scroll-margin-top: calc(var(--nav-h) - var(--bar-clear, 0px))" in _of(
        "#main-content", sheet=BAR
    )
    # One line at every width, a phone's islands included.
    assert "scroll-margin-top" not in _of("#main-content", "(max-width: 1099.98px)", BAR)
    nav = SHEET.with_name("_nav.css")
    assert (
        int(re.search(r"--nav-h: calc\((\d+)px", _of(":root", "(max-width: 699.98px)", nav))[1])
        >= 52
    )


ASK = SHEET.with_name("_watch_ask.css")


def test_tonight_s_controls_are_44px_to_press_inside_what_clips_them() -> None:
    assert "min-height: 38px" in _of(".tn__seg > button", sheet=ASK)
    assert "inset: -3.5px 0" in _of(".tn__seg > button::after", sheet=ASK)
    assert "min-height: 44px" in _of(".tn__foot", sheet=ASK)
    assert "align-self: stretch" in _of(".tn__foot > .txt", sheet=ASK)


TONIGHT = "_watch_ask.css"
TIMES = 5


def _seg_room(seg: dict[str, str]) -> float:
    """What the time picker's own padding takes of its width."""
    return 2 * px(seg["padding"])


def test_tonight_s_five_times_keep_44px_each_at_320() -> None:
    page = int(re.search(r"max\(clamp\((\d+)px,\s*4vw", token("--pad"))[1])
    assert page > 0.04 * 320  # the clamp's floor is the side padding at 320
    box = style(".tn", TONIGHT, "(max-width: 339.98px)")["padding-inline"]
    border = style(".tn", TONIGHT)["border"].split()[0]
    seg = style(".tn__seg", TONIGHT)
    left = 320 - 2 * page - 2 * px(box) - 2 * px(border) - _seg_room(seg)
    assert left >= TIMES * 44


def test_tonight_s_times_wrap_rather_than_overlap_where_five_44px_ones_do_not_fit() -> None:
    (query,) = {
        where[-1]
        for where, sels, decls in rules(TONIGHT)
        if ".tn__seg" in sels and decls.get("flex-wrap") == "wrap"
    }
    width = float(re.fullmatch(r"@container \(max-width: ([\d.]+)px\)", query)[1]) + 0.02
    assert width >= TIMES * 44 + _seg_room(style(".tn__seg", TONIGHT))
    assert style(".tn__seg", TONIGHT, query)["display"] == "flex"
    _, shrink, basis = style(".tn__seg > button", TONIGHT, query)["flex"].split()
    assert px(basis) >= 44 and shrink == "0"


def test_an_ai_pick_s_sparkle_has_a_backplate_of_its_own_in_forced_colours() -> None:
    spark = _of(".watch-card__spark", FORCED, ASK)
    assert "background: Canvas" in spark and "color: CanvasText" in spark


def test_a_question_s_head_on_a_phone_holds_two_lines_of_words_and_one_under_them() -> None:
    height = _last("min-height", _of(".ask__row", "(max-width: 599.98px)", ASK))
    two_lines_and_one = 2 * 1.3 * 16 + 1 + 1.35 * 13 + 2 * 8 + 2 * 1
    assert (
        eval(height.replace("calc", "").replace("px", ""), {"__builtins__": {}})
        >= two_lines_and_one
    )


REMOTE = SHEET.with_name("_tv_remote.css")
SEARCH = SHEET.with_name("_watch_search.css")
WIDE, PHONE = "(min-width: 700px)", "(max-width: 699.98px)"
BAR_SHOWN = "html.has-tv-mini"


def _last(name: str, css: str) -> str:
    return re.findall(rf"(?<![\w-]){re.escape(name)}:\s*([^;]+)", css)[-1]


def _bar_px(value: str) -> str:
    """The bar's own height: what `--nav-h` holds, less the inset at the top of a phone."""
    return re.sub(r"calc\((\d+px) \+ env\(safe-area-inset-top, 0px\)\)", r"\1", value)


def _px(value: str, narrow: bool, height: int) -> float:
    """What a length comes to in pixels in a window *height* high, on a phone if *narrow*."""
    nav = SHEET.with_name("_nav.css")
    nav_h = _bar_px(
        _last("--nav-h", _of(":root", PHONE, nav) if narrow else _of(":root", sheet=nav))
    )
    value = re.sub(r"env\([^()]*\)", "0px", value)
    # No phone has a bar at its foot.
    for name, length in (("--nav-h", nav_h), ("--seen", "100dvh")):
        value = re.sub(rf"var\({name}(?:, [^()]+)?\)", length, value)
    value = value.replace("var(--seen-top, 0px)", "0px").replace("calc", "")
    value = re.sub(r"([\d.]+)dvh", lambda m: str(float(m[1]) / 100 * height), value)
    return float(eval(value.replace("px", ""), {"__builtins__": {}, "min": min, "max": max}))


def _bar_top(narrow: bool, height: int) -> float:
    """How far the top of the TV bar is from the foot of the window."""
    css = _of(".tv-mini", sheet=REMOTE) + (_of(".tv-mini", PHONE, REMOTE) if narrow else "")
    pads = re.findall(r"(\d+)px", _last("padding", css))
    inside = _last("min-height", _of(".tv-mini__open", sheet=REMOTE))
    tall = int(pads[0]) + int(pads[2]) + _px(inside, narrow, height)
    return _px(_last("bottom", css), narrow, height) + tall


@pytest.mark.parametrize(
    ("narrow", "height"),
    [(False, 800), (False, 390), (True, 844), (True, 375)],
)
def test_the_search_sheet_ends_above_the_tv_bar_at_every_width(narrow, height) -> None:
    top = _of(".watch-sheet", sheet=SEARCH)
    if narrow:
        top += _of(".watch-sheet", PHONE, SEARCH)
    tallest = _of(f"{BAR_SHOWN} .watch-sheet", PHONE if narrow else WIDE, REMOTE)
    end = _px(_last("top", top), narrow, height) + _px(_last("max-height", tallest), narrow, height)
    assert end <= height - _bar_top(narrow, height)


@pytest.mark.parametrize("height", [844, 375])
def test_a_phone_s_tv_bar_is_off_the_foot_and_below_toasts_and_what_is_scrolled_to(height) -> None:
    bar = _of(".tv-mini", PHONE, REMOTE)
    assert _px(_last("bottom", bar), True, height) >= 8
    over = _bar_top(True, height)
    nav_h = int(
        _bar_px(_last("--nav-h", _of(":root", PHONE, SHEET.with_name("_nav.css")))).removesuffix(
            "px"
        )
    )
    assert over <= height - nav_h  # under the bar at the top
    toast = _of(f"{BAR_SHOWN}.has-watch-search .ui-toast-host", PHONE, REMOTE)
    assert _px(_last("bottom", toast), True, height) >= over
    scrolled = _of("html.has-tv-mini", PHONE, REMOTE)
    assert _px(_last("scroll-padding-bottom", scrolled), True, height) >= over


def _seconds(value: str) -> float:
    found = re.fullmatch(r"var\((--[\w-]+)\)", value)
    value = token(found[1]) if found else value
    return float(value.removesuffix("ms")) / 1000 if value.endswith("ms") else float(value[:-1])


def test_a_phone_card_s_badges_come_back_only_once_the_level_s_name_over_them_has_gone() -> None:
    back = style(".watch-card__poster > .watch-badge", "_watch_cards.css", "(hover: none)")
    gone = style(".rate__name > span", "_watch_cards.css")
    delay = re.search(r"opacity (\S+) \S+ (\S+)", back["transition"])
    fade = re.search(r"opacity (\S+)", gone["transition"])
    assert delay and fade and _seconds(delay[2]) >= _seconds(fade[1])


def test_a_lifted_card_s_poster_words_give_way_to_the_levels_and_their_names() -> None:
    lifted = ".is-lifted > .watch-card__link .watch-card__poster > "
    words = ":is(.watch-card__title, .watch-card__scores, .watch-badge)"
    pointer = "(hover: hover) and (pointer: fine)"
    assert style(lifted + words, "_watch_cards.css", pointer)["opacity"] == "0"


def test_a_preview_s_title_words_give_way_while_a_level_s_name_shows_over_them() -> None:
    named = ".pv:has(> .pv__lift > .rate--lift.is-open > .rate__name.is-on) .pv__head"
    assert style(named, "_watch_preview.css")["opacity"] == "0"
