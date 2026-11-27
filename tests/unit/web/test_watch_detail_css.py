"""What the title detail overlay's stylesheets have to hold for its parts to fit, stay reachable
and stay put, read as rules: selectors and declarations, per media query."""

from __future__ import annotations

import re
from pathlib import Path

from tests.unit.web._css import rgb, style, token

STYLES = Path(__file__).resolve().parents[3] / "src/mediaman/web/static/css"
SHEETS = ("_watch_detail.css", "_watch_cast.css")
PHONE = "(max-width: 699.98px)"


def _rules(media: str | None = None) -> list[tuple[list[str], str]]:
    """The (selectors, declarations) of the sheets outside any `@media`, or of those inside every
    `@media` whose query holds *media*."""
    css = "".join((STYLES / name).read_text(encoding="utf-8") for name in SHEETS)
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    blocks = re.findall(r"@media ([^{]+)\{((?:[^{}]*\{[^{}]*\})*)\s*\}", css)
    text = (
        "".join(body for query, body in blocks if media in query)
        if media
        else re.sub(r"@media [^{]+\{(?:[^{}]*\{[^{}]*\})*\s*\}", "", css)
    )
    return [
        ([s.strip() for s in head.split(",")], body)
        for head, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text)
    ]


def _of(selector: str, media: str | None = None) -> str:
    return "".join(body for selectors, body in _rules(media) if selector in selectors)


def test_a_phone_shows_which_episodes_are_watched_or_in_progress() -> None:
    assert "display: none" not in _of(".ttl .watch-ep__state", PHONE)
    assert "display: none" not in _of(".watch-ep__tick", PHONE)


def test_a_cut_off_description_opens_by_more_with_every_pointer_and_by_nothing_else() -> None:
    assert "display: none" in _of(".ttl-more")
    assert "display: block" in _of(".watch-eprow:has(> .watch-ep.is-long) > .ttl-more")
    opens = [
        selector
        for media in (None, "hover")
        for selectors, body in _rules(media)
        for selector in selectors
        if ".ttl-clamp" in selector and "max-height: var(--full" in body
    ]
    assert opens == [".is-open > .ttl-clamp", ".is-open > .watch-ep > .ttl-clamp"]


def test_the_buttons_of_a_title_held_never_stand_beside_the_thumbs_below_820() -> None:
    thumbs = ".ttl__main > :is(.rate"
    assert not any(h.startswith(thumbs) for hs, _ in _rules("(min-width: 700px)") for h in hs)
    assert any(h.startswith(thumbs) for hs, _ in _rules("(min-width: 820px)") for h in hs)


def test_a_season_to_be_ticked_is_tall_enough_for_a_finger() -> None:
    assert "min-height: 56px" in _of(".season-chip")
    assert "min-height: 44px" in _of(".season-pick__go")


def test_close_stands_above_a_trailer_and_not_on_it() -> None:
    sticky = "(max-width: 699.98px), (max-height: 500px)"
    assert "padding: var(--ttl-bar) 0" in _of(".ttl__player", sticky)
    assert "height: var(--ttl-bar)" in _of(".ttl__bar")
    # Elsewhere a strip slides up out of the sheet, and nothing that takes part in layout changes.
    wide = "(min-width: 700px) and (min-height: 500.02px)"
    assert "transform: translateY(var(--strip))" in _of(".ttl__sheet::before", wide)
    assert "transform: none" in _of(".ttl.is-trailer .ttl__sheet::before", wide)
    assert "overflow: visible" in _of(".ttl__sheet", wide)
    close = _of(".ttl.is-trailer .ttl__close", wide)
    assert "scale:" in close and "width" not in close
    # The strip ends at the window's top: a ring outside the button would be cut.
    assert re.search(r"outline-offset:\s*-", _of(".ttl.is-trailer .ttl__close:focus-visible", wide))


def test_close_stays_on_screen_on_a_phone_either_way_up() -> None:
    assert "position: sticky" in _of(".ttl__bar", "(max-width: 699.98px), (max-height: 500px)")
    # A scroll container of its own would hold the bar at the sheet's top instead.
    assert re.search(r"overflow:\s*clip", _of(".ttl__sheet"))


def test_close_never_stands_on_a_button_that_scrolls_under_it() -> None:
    sticky = "(max-width: 699.98px), (max-height: 500px)"
    assert "opacity: 1" in _of(".ttl.is-past .ttl__bar::before", sticky)
    # What the bar hides takes no presses through it.
    assert "pointer-events: auto" in _of(".ttl.is-past .ttl__bar", sticky)
    assert "scroll-padding-top: var(--ttl-bar)" in _of(".ttl", sticky)


def test_the_watched_button_s_icon_is_the_size_of_the_others() -> None:
    icons = ".ttl__actions .btn > .ico"
    assert _of(".ttl__actions .watch-mark-btn__faces .ico") == _of(icons) != ""


def test_the_thumbs_stand_out_of_the_flow_so_their_words_move_nothing() -> None:
    wide = {", ".join(heads): body for heads, body in _rules("(min-width: 820px)")}
    thumbs = wide[".ttl__main > :is(.rate, .ttl__rate-skel)"]
    beside = ":is(.ttl__actions, .ttl__under, .ttl__ratings)"
    room = wide[f".ttl__main:has(> :is(.rate, .ttl__rate-skel)) > {beside}"]
    assert "position: absolute" in thumbs
    held, kept = (int(re.findall(r"(?:width|right): ([1-9]\d*)px", b)[0]) for b in (thumbs, room))
    assert kept > held


def test_a_level_s_tip_is_drawn_over_the_hero_s_shade_it_reaches_up_into() -> None:
    wide = {", ".join(heads): body for heads, body in _rules("(min-width: 820px)")}
    thumbs = re.search(r"z-index: (\d+)", wide[".ttl__main > :is(.rate, .ttl__rate-skel)"])
    [shade] = [b for heads, b in _rules() if ".ttl__head" in heads and "z-index" in b]
    assert thumbs and int(thumbs[1]) > int(re.findall(r"z-index: (\d+)", shade)[0])


def test_a_title_on_its_way_out_takes_no_second_press_of_close() -> None:
    assert "pointer-events: none" in _of(".ttl.is-leaving > *")


def test_the_line_held_for_ratings_draws_nothing_in_any_colours() -> None:
    every = [
        selector
        for media in (None, "(forced-colors: active)")
        for selectors, _ in _rules(media)
        for selector in selectors
    ]
    # Nothing stands in it for a rating the title may not have, so forced colours have nothing to outline.
    assert not [
        s for s in every if s.startswith(".ttl__ratings-wait") and s != ".ttl__ratings-wait"
    ]
    assert not re.search(r"background|border|outline|box-shadow", _of(".ttl__ratings-wait"))
    assert not _of(".ttl__ratings-wait", "(forced-colors: active)")


def test_metascore_is_said_shorter_on_a_phone_and_still_read_in_full() -> None:
    assert "clip-path: inset(50%)" in _of(".ttl-rating__full", PHONE)
    assert "attr(data-brief)" in _of(".ttl-rating__brief::after", PHONE)
    assert not _of(".ttl-rating__full") + _of(".ttl-rating__brief::after")


def test_what_waits_for_room_comes_at_once_where_nothing_moves() -> None:
    for line in (
        ".season-pick__why.fade-in",
        ".season-pick__out > .fade-in",
        ".ttl__under > .fade-in",
    ):
        assert "animation-delay: var(--dur)" in _of(line)
        assert "animation-delay: 0s" in _of(line, "prefers-reduced-motion")
    # An error kept for its room alone is not seen.
    assert "opacity: 0" in _of(".ttl__error[inert]") and "opacity: 0" in _of(".ttl__note[inert]")


def test_a_page_opened_over_a_playing_trailer_never_shows_its_own_picture_first() -> None:
    film = ".ttl.is-film:not(.is-melting) .ttl__hero > :is(.ttl__art, .ttl__fill)"
    rules = [body for selectors, body in _rules() if ", ".join(selectors) == film]
    assert len(rules) == 1
    assert "opacity: 0" in rules[0] and "transition: none" in rules[0]


def test_the_dim_behind_a_title_sent_aside_holds_then_fades_before_the_sheet_goes() -> None:
    # The sheet is hidden --dur-slow (250 ms) after it goes aside: the dim's delay and fade fit in that.
    tokens = {"--dur-fast": 150, "--dur": 200, "--dur-slow": 250}
    rule = re.search(
        r"transition:\s*opacity var\((--dur[-a-z]*)\) [^;]*? (\d+)ms",
        _of(".ttl.is-aside + .ttl__dim"),
    )
    assert rule, "no held fade"
    delay = int(rule.group(2))
    assert delay >= 80
    assert delay + tokens[rule.group(1)] <= tokens["--dur-slow"]


def test_a_trailers_loading_mark_hides_the_frame_s_own_page_and_takes_no_clicks() -> None:
    mark = style(".ttl__wait", "_watch_detail.css")
    assert mark["pointer-events"] == "none"
    # Opaque, so an error page the frame shows before the trailer never shows through.
    fill = re.fullmatch(r"var\((--[\w-]+)\)", mark["background"])
    assert fill
    colour = rgb(token(fill[1]))
    assert len(colour) == 3 or colour[3] == 1, colour
