"""What the Watch pages' stylesheet has to hold for its badges and toggles to be seen, read as rules."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SHEET = Path(__file__).resolve().parents[3] / "src/mediaman/web/static/css/_watch.css"
FORCED = "(forced-colors: active)"


def _of(selector: str, media: str | None = None, sheet: Path = SHEET) -> str:
    """The declarations of *selector* outside any `@media`, or inside those asking for *media*."""
    css = re.sub(r"/\*.*?\*/", "", sheet.read_text(encoding="utf-8"), flags=re.S)
    blocks = r"@media ([^{]+)\{((?:[^{}]*\{[^{}]*\})*)\s*\}"
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


def test_a_leaving_badge_says_the_days_left() -> None:
    assert "display: none" in _of(".watch-badge__long")
    assert "display: none" not in _of(".watch-badge__short")
    wider = "max-width: 599.98px"
    assert not any(_of(s, wider) for s in (".watch-badge__long", ".watch-badge__short"))


def test_a_leaving_badge_shows_fewer_words_wherever_the_short_ones_do_not_fit() -> None:
    window = _of(".watch-badge--leaving")
    assert "flex-wrap: wrap" in window and "overflow: hidden" in window
    # The fewer words take no width of the line, so the short ones alone say whether it wraps;
    # where it does not, they stand at the foot of a line two high, out of the window.
    for words in (".watch-badge__tiny", ".watch-badge__least"):
        assert "align-self: flex-end" in _of(words)
        assert "attr(data-words)" in _of(f"{words}::after")
    tiny = _of(".watch-badge__tiny")
    assert "flex: 0 0 100%" in tiny and "margin-right: -100%" in tiny
    assert "width: 0" in _of(".watch-badge__least")
    for words in (".watch-badge__short", ".watch-badge__tiny::after"):
        assert "flex: none" in _of(words) and "margin-bottom: var(--badge-h)" in _of(words)
    # Read out in full whatever is drawn.
    assert "display: none" not in _of(".watch-badge__short", "max-width: 359.98px")


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


def test_forced_colours_give_a_download_s_hidden_art_no_room() -> None:
    sheet = SHEET.with_name("_dl.css")
    for art in (".dl-hero-bg", ".dl-hero-overlay", ".dl-hero-scrim", ".dl-hero-poster"):
        assert "display: none" in _of(art, FORCED, sheet)
    assert "min-height: 0" in _of(".dl-hero", FORCED, sheet)


def test_forced_colours_give_keep_an_edge_and_a_hidden_picture_no_room() -> None:
    assert "border: 1px solid" in _of(".watch-keep::before", FORCED)
    banner = _of(".watch-banner:has(> .watch-banner__bg--art:not([data-broken]))", FORCED)
    assert "min-height: 0" in banner
    detail = SHEET.with_name("_watch_detail.css")
    assert "min-height: 190px" in _of(".ttl:not(.is-trailer) .ttl__hero", FORCED, detail)


def test_a_leaving_badge_has_an_edge_in_forced_colours() -> None:
    # Round its words, inside the window that would cut it off, and not over the poster's width.
    for pill in (".watch-badge__short", ".watch-badge__tiny::after", ".watch-badge__least::after"):
        drawn = _of(pill, FORCED)
        assert "outline: 1px solid CanvasText" in drawn and "outline-offset: -1px" in drawn
        assert "background: Canvas" in drawn
    assert not _of(".watch-badge--leaving", FORCED)


def test_a_caption_is_shortened_on_a_phone_alone() -> None:
    assert "clip-path" not in _of(".watch-card__wide") + _of(".watch-card__wide", "1279.98px")
    assert "clip-path" in _of(".watch-card__wide", "max-width: 599.98px")
    assert "attr(data-unit)" in _of(".watch-card__unit::after", "max-width: 599.98px")


def test_a_caption_with_no_room_for_both_parts_shows_the_tail_and_in_search_the_kind() -> None:
    caption = ".watch-card__sub:has(> .watch-card__lead)"
    assert "flex-wrap: wrap-reverse" in _of(caption) and "height: 1lh" in _of(caption)
    assert "flex-wrap: wrap;" in _of(f".results-grid {caption}")


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


def test_what_is_being_done_of_a_title_stays_on_one_line_beside_its_thumb() -> None:
    grid = SHEET.with_name("_watch_grid.css")
    assert "white-space: nowrap" in _of(".watch-card.is-working .watch-card__sub", sheet=grid)


def test_every_button_under_a_card_takes_44px_on_a_touch_screen() -> None:
    css = CARDS.read_text(encoding="utf-8")
    size = re.search(r"\.card-tool \{[^}]*?width: (\d+)px;\s*height: (\d+)px", css)
    grown = re.search(r"\.card-tool::after \{[^}]*inset: -(\d+)px -(\d+)px -(\d+)px -(\d+)px", css)
    assert size and grown
    top, right, bottom, left = map(int, grown.groups())
    assert int(size.group(1)) + left + right >= 44 and int(size.group(2)) + top + bottom >= 44
    # Four levels of 44px and the 4px round them.
    assert "--h: max(184px," in css


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


MOUSE = "(hover: hover) and (pointer: fine)"


def test_with_a_mouse_the_buttons_of_a_card_far_out_of_sight_are_not_drawn() -> None:
    far = ".watch-card.is-far > .card-tools"
    assert "visibility: hidden" in _of(far, MOUSE, CARDS)
    # A touch screen's thumbs, drawn at rest, are as they were.
    assert not _of(far, sheet=CARDS) and not _of(far, FINGER, CARDS)


def test_with_a_mouse_a_card_a_finger_pressed_shows_its_buttons_as_under_the_mouse() -> None:
    shown = (
        ".watch-card:is(:hover, .has-focus, .is-lifted, .is-touched) > "
        ":where(.watch-card__link:not(.is-previewed)) ~ :where(.card-tools) > .card-tool"
    )
    assert "opacity: 1; transform: none" in _of(shown, MOUSE, CARDS)
    ring = ".watch-card:is(:hover, .has-focus, .is-lifted, .is-touched) .card-tool::before"
    assert "opacity: 1" in _of(ring, MOUSE, CARDS)


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
    # Nothing round it on its way over the picture.
    assert "display: none" in _of(".card-flier .halo", sheet=CARDS)


def test_the_preview_s_eye_is_filled_once_watched_as_the_panel_s_is() -> None:
    eye = _of('.pv__eye[aria-pressed="true"]', sheet=PREVIEW)
    assert "background: var(--t1); color: var(--bg)" in eye
    assert "background: var(--t1)" in _of('.card-over__b[aria-pressed="true"]::before', sheet=CARDS)


def test_the_icons_under_a_card_do_not_outgrow_their_circles_with_the_text() -> None:
    for icon in (".card-tool > .ico", ".card-over__b > .ico"):
        assert re.search(r"width: [\d.]+px; height: \d+px", _of(icon, sheet=CARDS))


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


def test_a_card_s_title_eases_down_as_its_bar_goes() -> None:
    assert "padding-bottom var(--dur)" in _of(".watch-card__poster")
    bar = _of(".card-barend", "(prefers-reduced-motion: no-preference)", CARDS)
    assert "bar-away" in bar


def test_the_levels_are_readable_where_the_picture_under_them_is_not_blurred() -> None:
    ground = re.findall(r"var\(--s2\) (\d+)%", _of(".rate--lift .rate__set", sheet=CARDS))
    assert int(ground[0]) >= 90


def test_an_eye_that_had_shut_while_it_waited_opens_and_is_not_shut_twice() -> None:
    motion = "(prefers-reduced-motion: no-preference)"
    for part in (".pv__eye.is-blink > .ico", '.is-blink[aria-pressed="true"]'):
        assert "var(--from-shut)" in _of(part, motion, CARDS)
    assert "var(--shut, 0)" in _of(".is-blink", motion, CARDS)


def test_an_episode_s_caption_drops_left_and_then_the_time_and_keeps_the_episode() -> None:
    caption = ".watch-card__sub:has(> .watch-card__left)"
    assert "flex-wrap: wrap;" in _of(caption)
    # A dot after the episode would stay behind when the time goes.
    assert "content: none" in _of(f"{caption} > .watch-card__lead::after")
    assert "\\a0·\\a0" in _of(f"{caption} > .watch-card__tail::before")


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


def test_a_rows_offer_covers_no_heading_and_no_words_of_see_all_on_a_phone() -> None:
    browse = SHEET.with_name("_watch_browse.css")
    phone = "(max-width: 599.98px)"
    for media in (None, phone):
        chip = _of(".row-open__cue::before", media, browse)
        # As wide as the room See all keeps for it, and under its words.
        assert re.findall(r"inset: -?\d+px 0;", chip) or (media and "inset" not in chip)
        assert media or "z-index: -1" in chip
        assert "position: absolute" not in _of(".row-open__cue", media, browse)
    assert "isolation: isolate" in _of(".row-open__cue", sheet=browse)
    assert int(re.findall(r"padding: 0 (\d+)px;", _of(".row-open__cue", sheet=browse))[0]) >= 8
    # Beside the row's own tools or link a phone has no room for the words.
    phone_rules = re.sub(r"/\*.*?\*/", "", browse.read_text(encoding="utf-8"), flags=re.S)
    assert re.search(
        r":has\(> :is\(\.watch-row__tools, \.space-row-link\)\) "
        r":is\(\.row-open__word, \.row-open__total\) \{ display: none; \}",
        phone_rules,
    )


def test_the_words_of_see_all_keep_their_colour_under_the_pointer_in_forced_colours() -> None:
    browse = SHEET.with_name("_watch_browse.css")
    for state in ("", ":hover", ":active"):
        rules = _of(f".row-open[href]{state} .row-open__cue", FORCED, browse)
        assert "color: LinkText" in rules


def test_a_person_s_photo_stays_in_the_strip_while_their_card_is_open() -> None:
    cast = SHEET.with_name("_watch_cast.css")
    for media in (None, "prefers-reduced-motion"):
        for part in (" .cast-face", " > .ttl-cast__avatar"):
            rules = _of(f".ttl-cast__item.is-out{part}", media, cast)
            assert "hidden" not in rules and "opacity" not in rules
    assert not _of(".ttl-cast__avatar::before", sheet=cast)


def test_the_badges_under_a_leaving_one_stand_in_line_with_it() -> None:
    browse = SHEET.with_name("_watch_browse.css")
    under = ".watch-card__poster > .watch-badge--leaving ~ .watch-badge:not(.watch-badge--leaving)"
    for media in (None, "(max-width: 599.98px)"):
        left = re.findall(r"left: \d+px", _of(".watch-badge--leaving", media))
        assert left and re.findall(r"left: \d+px", _of(under, media, browse)) == left
    assert "right: auto" in _of(under, sheet=browse)


def test_a_watched_badge_stands_under_any_other_in_its_corner() -> None:
    under = re.search(
        r"\.watch-card__poster > :is\(([^)]*)\) ~ \.watch-badge--watched \{ top: 36px; \}",
        SHEET.read_text(encoding="utf-8"),
    )
    assert under
    assert {".watch-badge--leaving", ".watch-badge--state", ".watch-badge--new"} == set(
        under[1].split(", ")
    )


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
    css = BAR.read_text(encoding="utf-8")
    rules = re.findall(r"([^{}]*)\{ scroll-margin-top: [^}]*\}", css)
    page = [r.strip() for r in rules if CONTROLS in r]
    assert len(page) == 2
    for selector in page:
        assert selector.endswith(':not(.watch-all__head *, [role="menu"] *)')
    sticky = {
        head.strip()
        for sheet in SHEET.parent.glob("_watch*.css")
        for head, body in re.findall(r"([^{}]+)\{([^{}]*)\}", sheet.read_text(encoding="utf-8"))
        if "position: sticky" in body
    }
    # The bar of a title's overlay is in #watch-detail, which the rule leaves out.
    assert sticky == {".watch-all__head", ".ttl__bar"}


def test_the_skip_link_lands_clear_of_the_bar_at_every_width() -> None:
    assert "scroll-margin-top: var(--nav-h)" in _of("#main-content", sheet=BAR)
    # One line at every width, and nothing at the top of a phone.
    assert "scroll-margin-top" not in _of("#main-content", "(max-width: 1099.98px)", BAR)
    nav = SHEET.with_name("_nav.css")
    assert "--nav-h: 0px" in _of("html:has(.nav-foot)", "(max-width: 699.98px)", nav)


ASK = SHEET.with_name("_watch_ask.css")


def test_tonight_s_controls_are_44px_to_press_inside_what_clips_them() -> None:
    assert "min-height: 38px" in _of(".tn__seg > button", sheet=ASK)
    assert "inset: -3.5px 0" in _of(".tn__seg > button::after", sheet=ASK)
    assert "min-height: 44px" in _of(".tn__foot", sheet=ASK)
    assert "align-self: stretch" in _of(".tn__foot > .txt", sheet=ASK)


def test_the_panel_s_headings_and_a_film_s_length_stay_on_one_line_with_large_text() -> None:
    assert "flex: none" in _of(".watch-sheet__head > .watch-sheet__title", sheet=ASK)
    assert "white-space: nowrap" in _of(".tn__art > span", sheet=ASK)
    # The picture is as wide as it needs.
    assert "position: relative" in _of(".tn__art > span", sheet=ASK)
    assert "min-width: 104px" in _of(".tn__art", "(max-width: 599.98px)", ASK)


REMOTE = SHEET.with_name("_tv_remote.css")
SEARCH = SHEET.with_name("_watch_search.css")
WIDE, PHONE = "(min-width: 700px)", "(max-width: 699.98px)"
BAR_SHOWN = "body:has(> .tv-mini:not([hidden]))"


def _last(name: str, css: str) -> str:
    return re.findall(rf"(?<![\w-]){re.escape(name)}:\s*([^;]+)", css)[-1]


def _px(value: str, foot: int, height: int) -> float:
    """What a length of a window *height* high with *foot* at its foot comes to in pixels."""
    wide_nav = _last("--nav-h", _of(":root", sheet=SHEET.with_name("_tokens.css")))
    value = re.sub(r"env\([^()]*\)", "0px", value)
    for name, px in (("--foot", f"{foot}px"), ("--nav-h", wide_nav), ("--seen", "100dvh")):
        value = re.sub(rf"var\({name}(?:, [^()]+)?\)", px, value)
    value = value.replace("var(--seen-top, 0px)", "0px").replace("calc", "")
    value = re.sub(r"([\d.]+)dvh", lambda m: str(float(m[1]) / 100 * height), value)
    return float(eval(value.replace("px", ""), {"__builtins__": {}, "min": min, "max": max}))


def _bar_top(narrow: bool, foot: int, height: int) -> float:
    """How far the top of the TV bar is from the foot of the window."""
    css = _of(".tv-mini", sheet=REMOTE) + (_of(".tv-mini", PHONE, REMOTE) if narrow else "")
    pads = re.findall(r"(\d+)px", _last("padding", css))
    inside = _last("min-height", _of(".tv-mini__open", sheet=REMOTE))
    tall = int(pads[0]) + int(pads[2]) + _px(inside, foot, height)
    return _px(_last("bottom", css), foot, height) + tall


@pytest.mark.parametrize(
    ("narrow", "foot", "height"),
    [(False, 0, 800), (False, 0, 390), (True, 72, 844), (True, 60, 375)],
)
def test_the_search_sheet_ends_above_the_tv_bar_at_every_width(narrow, foot, height) -> None:
    top = _of(".watch-sheet", sheet=SEARCH)
    if narrow:
        top += _of(".watch-sheet", PHONE, SEARCH)
    tallest = _of(f"{BAR_SHOWN} .watch-sheet", PHONE if narrow else WIDE, REMOTE)
    end = _px(_last("top", top), foot, height) + _px(_last("max-height", tallest), foot, height)
    assert end <= height - _bar_top(narrow, foot, height)


@pytest.mark.parametrize("foot", [60, 72])
def test_a_phone_s_tv_bar_is_above_the_foot_and_below_toasts_and_what_is_scrolled_to(foot) -> None:
    bar = _of(".tv-mini", PHONE, REMOTE)
    assert _px(_last("bottom", bar), foot, 844) >= foot
    over = _bar_top(True, foot, 844)
    toast = _of(f"{BAR_SHOWN}:has(.watch-search) .ui-toast-host", PHONE, REMOTE)
    assert _px(_last("bottom", toast), foot, 844) >= over
    scrolled = _of("html:has(.tv-mini:not([hidden]))", PHONE, REMOTE)
    assert _px(_last("scroll-padding-bottom", scrolled), foot, 844) >= over


def test_an_error_page_on_a_phone_is_as_high_as_the_screen_less_the_nav() -> None:
    nav = _last("--nav-h", _of(".nav-glass", PHONE, SHEET.with_name("_nav.css")))
    page = _of("body:has(> .nav-glass) .login-page", PHONE, SHEET.with_name("_error.css"))
    assert _last("min-height", page) == f"calc(100vh - {nav})"


def test_a_row_kept_at_its_start_does_not_snap_whatever_its_script_sets_inline() -> None:
    rule = _of(".watch-row__scroll.is-still", sheet=SHEET.parent / "_watch_downloading.css")
    assert "scroll-snap-type: none !important" in rule
