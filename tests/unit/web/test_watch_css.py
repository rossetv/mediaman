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
        if selector in [s.strip() for s in head.split(",")]
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
    (SHEET, '.watch-viewtoggle__btn[aria-current="page"]::before'),
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
    assert "border: 1px solid" in _of(".keep-trigger", FORCED, SHEET.with_name("_buttons.css"))
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


def test_a_caption_ends_before_the_more_of_any_card_that_has_one() -> None:
    caption = ".watch-card:has(> .watch-more) .watch-card__sub"
    wide = re.search(r"padding-right: (\d+)px", _of(caption))
    phone = re.search(r"padding-right: (\d+)px", _of(caption, "max-width: 599.98px"))
    # The dots are 14px wide: in the middle of 44px that end 8px past the card, on a phone 7px from that end.
    assert wide and int(wide.group(1)) >= 44 / 2 + 14 / 2 - 8
    assert phone and int(phone.group(1)) >= 14 + 7 - 8
    assert "data-mark" not in "".join(
        line for line in SHEET.read_text(encoding="utf-8").splitlines() if "__sub" in line
    )


def test_the_ring_and_the_disc_of_more_stand_around_the_dots_on_a_phone() -> None:
    phone = "max-width: 599.98px"
    more = _of(".watch-more", phone)
    _, right, _, left = (int(v) for v in re.findall(r"(\d+)px", _of(".watch-more::before", phone)))
    dots = int(re.findall(r"font-size: (\d+)px", _of(".watch-more"))[0])
    edge = int(re.findall(r"padding-right: (\d+)px", more)[0])
    # An icon is 1.25em wide.
    assert abs(edge + dots * 1.25 / 2 - (right + (44 - right - left) / 2)) < 0.5
    assert "outline: none" in _of(".watch-more:focus-visible", phone)
    ring = _of(".watch-more:focus-visible::before", phone)
    assert "outline: 2px solid" in ring and "opacity: 1" in ring


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


def test_a_rows_offer_covers_no_heading_or_its_link_on_a_phone() -> None:
    browse = SHEET.with_name("_watch_browse.css")
    text = _of(".row-offer__text", "(max-width: 499.98px)", browse)
    assert "position: absolute" in text and "clip-path: inset(50%)" in text


def test_a_person_s_photo_stays_in_the_strip_while_their_card_is_open() -> None:
    cast = SHEET.with_name("_watch_cast.css")
    for media in (None, "prefers-reduced-motion"):
        for part in (" .cast-face", " > .ttl-cast__avatar"):
            rules = _of(f".ttl-cast__item.is-out{part}", media, cast)
            assert "hidden" not in rules and "opacity" not in rules
    assert not _of(".ttl-cast__avatar::before", sheet=cast)


def test_the_badges_under_a_leaving_one_stand_in_line_with_it() -> None:
    browse = SHEET.with_name("_watch_browse.css")
    under = (
        ".watch-card__poster:has(> .watch-badge--leaving) > .watch-badge:not(.watch-badge--leaving)"
    )
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
