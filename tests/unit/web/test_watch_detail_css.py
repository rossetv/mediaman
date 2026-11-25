"""What the title detail overlay's stylesheets have to hold for its parts to fit, stay reachable
and stay put, read as rules: selectors and declarations, per media query."""

from __future__ import annotations

import re
from pathlib import Path

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
    assert '". thumb state"' in _of(".ttl .watch-ep", PHONE)


def test_a_long_season_name_wraps_inside_its_chip() -> None:
    assert "min-width: 0" in _of(".season-chip__name")
    assert "nowrap" not in _of(".season-chip", PHONE)


def test_a_long_cast_name_wraps_inside_its_own_column() -> None:
    body = _of(".ttl-cast__name")
    assert "overflow-wrap: anywhere" in body


def test_a_trailer_s_bar_stands_over_the_video_and_not_on_it() -> None:
    assert "padding: var(--ttl-bar) 0" in _of(".ttl__player")
    assert "height: var(--ttl-bar)" in _of(".ttl__bar")


def test_a_trailer_s_frame_never_stands_taller_than_its_box() -> None:
    body = _of(".ttl__frame")
    assert "height: 100%" in body and "aspect-ratio" not in body


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


def test_the_space_of_a_tagline_a_title_lacks_goes_under_its_meta_line() -> None:
    assert "order: 1" in _of(".ttl__tagline:empty")
    assert "min-height" in _of(".ttl__tagline")


def test_the_admin_s_keep_and_delete_share_the_panel_s_width_equally() -> None:
    assert "grid-auto-columns: minmax(0, 1fr)" in _of(".ttl-facts__foot")


def test_the_seasons_of_a_show_never_wrap_onto_a_second_line() -> None:
    assert "flex-wrap: nowrap" in _of(".ttl .watch-seasons")
    assert "flex: none" in _of(".ttl .watch-pill")


def test_the_watched_button_s_icon_is_the_size_of_the_others() -> None:
    icons = ".ttl__actions .btn > .ico"
    assert _of(".ttl__actions .watch-mark-btn__faces .ico") == _of(icons) != ""


def test_a_button_s_label_never_wraps_on_a_phone() -> None:
    # Each is as wide as its label at least, and a row takes as many as fit it.
    assert "flex: 1 1 120px" in _of(".ttl__actions > .btn", PHONE)
    assert "min-width: max-content" in _of(".ttl__actions > .btn", PHONE)
    assert "grid" not in _of(".ttl__actions", PHONE)
    assert "flex-wrap: wrap" in _of(".ttl__actions")
    assert "white-space" not in _of(".ttl__actions .watch-mark-btn", PHONE)


def test_the_facts_labels_take_the_room_there_is_where_the_panel_is_wide() -> None:
    wide = "(min-width: 360px) and (max-width: 899.98px)"
    assert "grid-template-columns: max-content" in _of(".ttl-facts__list", wide)


def test_a_title_on_its_way_out_takes_no_second_press_of_close() -> None:
    assert "pointer-events: none" in _of(".ttl.is-leaving > *")


def test_a_tagline_on_its_way_holds_a_whole_line() -> None:
    # Its bar is less than a line tall, so the margins make up the rest.
    body = _of(".ttl__tagline-skel")
    assert "height: .8em" in body
    assert "margin-block: calc((1lh - .8em) / 2)" in body


def test_the_facts_labels_beside_a_title_have_one_width_before_and_after_they_come() -> None:
    assert "grid-template-columns: 112px " in _of(".ttl-facts__list")


def test_a_rating_still_to_come_shows_no_figure_and_is_outlined_in_forced_colours() -> None:
    assert "visibility: hidden" in _of(".ttl__ratings-wait > .ttl-rating > *")
    forced = _of(".ttl__ratings-wait > .ttl-rating", "(forced-colors: active)")
    assert "outline: 1px solid" in forced


def test_metascore_is_said_shorter_on_a_phone_and_still_read_in_full() -> None:
    assert "clip-path: inset(50%)" in _of(".ttl-rating__full", PHONE)
    assert "attr(data-brief)" in _of(".ttl-rating__brief::after", PHONE)
    assert not _of(".ttl-rating__full") + _of(".ttl-rating__brief::after")


def test_the_ratings_still_to_come_hold_one_line() -> None:
    line = _of(".ttl__ratings-wait")
    assert "height: calc(1lh + 8px)" in line
    assert "overflow: hidden" in line
