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
    assert "display: none" not in _of(".watch-ep__tick", PHONE)


def test_a_phone_gives_an_episodes_description_the_width_under_its_picture() -> None:
    assert '"about about about"' in _of(".ttl .watch-ep", PHONE)


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


def test_a_text_that_closes_is_not_masked_under_its_second_line() -> None:
    assert "0 2.9em / 100% calc(100% - 2.9em)" in _of(".is-long > .ttl-clamp")
    assert "mask: none" not in _of(".is-open > .watch-ep > .ttl-clamp")
    assert "--ttl-tail" in _of(".ttl-clamp").partition("transition:")[2]


def test_what_stands_in_for_a_season_stands_where_its_parts_do() -> None:
    assert "grid-row: 3" in _of(".ttl__season > .watch-episodes")
    assert "grid-area: 3 / 1" in _of(".ttl__season > .ttl-epskels")


def test_a_season_pill_changes_its_words_half_way_through_its_ground() -> None:
    assert "color 0s calc(var(--dur-fast) / 2)" in _of(".ttl .watch-pill")


def test_the_buttons_of_a_title_held_never_stand_beside_the_thumbs_below_820() -> None:
    thumbs = ".ttl__main > :is(.rate"
    assert not any(h.startswith(thumbs) for hs, _ in _rules("(min-width: 700px)") for h in hs)
    assert any(h.startswith(thumbs) for hs, _ in _rules("(min-width: 820px)") for h in hs)


def test_a_phone_has_the_collections_button_on_its_headings_line() -> None:
    css = (STYLES / "_watch_collection.css").read_text(encoding="utf-8")
    phone = css.partition(f"@media {PHONE}")[2].partition("/* ── The dialog")[0]
    assert "grid-template-columns: minmax(0, 1fr) auto" in phone
    assert ".coll__get-full { display: none; }" in phone


def test_a_long_season_name_wraps_inside_its_chip() -> None:
    assert "min-width: 0" in _of(".season-chip__name")
    assert "nowrap" not in _of(".season-chip", PHONE)


def test_a_season_to_be_ticked_is_tall_enough_for_a_finger() -> None:
    assert "min-height: 56px" in _of(".season-chip")
    assert "min-height: 44px" in _of(".season-pick__go")


def test_a_long_cast_name_wraps_inside_its_own_column() -> None:
    body = _of(".ttl-cast__name")
    assert "overflow-wrap: break-word" in body


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


def test_what_a_trailer_takes_away_fades_before_it_is_hidden() -> None:
    css = (STYLES / "_watch_detail.css").read_text(encoding="utf-8")
    for hides in (":is(.ttl__genre, .ttl__director)", ".ttl__trailer > span"):
        rule = re.escape(hides) + r" \{[^}]*transition:[^;]*visibility var\(--dur"
        assert re.search(rule, css), hides


def test_a_trailer_s_frame_is_16_to_9_and_never_larger_than_its_box() -> None:
    body = _of(".ttl__frame")
    assert "width: min(100cqw, 100cqh * 16 / 9)" in body and "aspect-ratio: 16 / 9" in body
    assert "container-type: size" in _of(".ttl__player")


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


def test_the_thumbs_stand_out_of_the_flow_so_their_words_move_nothing() -> None:
    wide = {", ".join(heads): body for heads, body in _rules("(min-width: 820px)")}
    thumbs = wide[".ttl__main > :is(.rate, .ttl__rate-skel)"]
    beside = ":is(.ttl__actions, .ttl__under, .ttl__ratings)"
    room = wide[f".ttl__main:has(> :is(.rate, .ttl__rate-skel)) > {beside}"]
    assert "position: absolute" in thumbs
    held, kept = (int(re.findall(r"(?:width|right): ([1-9]\d*)px", b)[0]) for b in (thumbs, room))
    assert kept > held


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


def test_the_ratings_still_to_come_hold_one_line_beside_those_already_there() -> None:
    line = _of(".ttl__ratings-wait")
    assert "height: calc(1lh + 8px)" in line
    assert "font-size: 12px" in line  # the pills' own, so 1lh is their line
    assert "flex: 1 1 0" in line and "min-width: 0" in line


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


def test_a_season_that_cannot_be_asked_for_has_no_box() -> None:
    assert _of('.season-chip[data-state="some"] .season-chip__box') == ""
    assert "border-color: transparent" in _of(
        '.season-chip[aria-disabled="true"] .season-chip__box'
    )


def test_a_season_that_cannot_be_asked_for_keeps_no_empty_room_for_a_box() -> None:
    empty = '.season-chip[aria-disabled="true"][data-state="none"][data-icon="i-check"]'
    assert "display: none" in _of(f"{empty} .season-chip__box")
    assert "grid-template-columns: minmax(0, 1fr)" in _of(empty)


def test_the_button_that_asks_eases_in_to_its_own_opacity() -> None:
    assert "animation-name: season-go-in" in _of(".season-pick__go.fade-in")
    css = (STYLES / "_watch_detail.css").read_text(encoding="utf-8")
    frames = re.search(r"@keyframes season-go-in \{(.*)\}$", css, re.M)
    assert frames and re.findall(r"(\w+) \{", frames[1]) == ["from"]


def test_keep_has_the_room_beside_a_shows_wider_words_and_a_films_row_stays_as_it_is() -> None:
    row = ".ttl__actions.has-play-on"
    wide = "(min-width: 700px)"
    films = [s for selectors, body in _rules(wide) if "max-width: 500px" in body for s in selectors]
    shows = [s for selectors, body in _rules(wide) if "max-width: 530px" in body for s in selectors]
    assert films and not any("show" in s for s in films)
    assert shows == [
        f'{row}:has(> .watch-btn--keep):has(> [data-mark-toggle^="show/"])',
        f"{row}:has(> .ttl__keep-skel--show)",
    ]


def test_a_page_opened_over_a_playing_trailer_never_shows_its_own_picture_first() -> None:
    film = ".ttl.is-film:not(.is-melting) .ttl__hero > :is(.ttl__art, .ttl__fill)"
    rules = [body for selectors, body in _rules() if ", ".join(selectors) == film]
    assert len(rules) == 1
    assert "opacity: 0" in rules[0] and "transition: none" in rules[0]


def test_a_downloads_sizes_line_holds_its_room_until_they_are_known() -> None:
    assert "min-height: 1lh" in _of(".dl-progress__line")


def test_an_episode_fact_stays_whole_where_it_fits_and_breaks_rather_than_overflow() -> None:
    fact = _of(".ttl .watch-ep__fact")
    # Laid out whole, it moves to a line of its own before it is broken; only one wider than
    # the column is broken inside.
    assert "display: inline-block" in fact
    assert "max-width: 100%" in fact
    every = "".join((STYLES / name).read_text(encoding="utf-8") for name in (*SHEETS, "_watch.css"))
    rules = re.findall(r"([^{}]*watch-ep__fact[^{}]*)\{([^{}]*)\}", every)
    assert rules and not [body for _, body in rules if "nowrap" in body]


def test_a_trailers_loading_mark_covers_its_frame_and_takes_no_clicks() -> None:
    mark = _of(".ttl__wait")
    assert "grid-area: 1 / 1" in mark  # the frame's own cell, drawn after it, so it is on top
    assert "background: var(--scrim)" in mark  # opaque, so the frame's error page never shows
    assert "pointer-events: none" in mark
    assert "width: min(100cqw, 100cqh * 16 / 9)" in mark and "aspect-ratio: 16 / 9" in mark


def test_the_lines_of_how_a_title_stands_differ_in_colour_alone() -> None:
    assert "font-size: 14px" in _of(".ttl__status")
    for line in (".ttl__leaving", ".ttl__status--warn"):
        own = {d.split(":")[0].strip() for d in _of(line).split(";") if d.strip()}
        assert own == {"color"}, (line, own)
