"""A field's edge and the room between fields, the bars the keyboard must stay clear of, History's row of
kinds, and All titles' grid on a phone, read as rules."""

from __future__ import annotations

import re
from pathlib import Path

CSS = Path(__file__).resolve().parents[3] / "src/mediaman/web/static/css"
CONTROLS = ":is(a, button, input, select, textarea, [tabindex])"


def _rules(sheet: str, media: str | None = None) -> list[tuple[str, str]]:
    """(selector list, declarations) outside any `@media`, or inside those asking for *media*."""
    css = re.sub(r"/\*.*?\*/", "", (CSS / sheet).read_text(encoding="utf-8"), flags=re.S)
    blocks = r"@media ([^{]+)\{((?:[^{}]*\{[^{}]*\})*)\s*\}"
    text = (
        "".join(body for query, body in re.findall(blocks, css) if media in query)
        if media
        else re.sub(blocks, "", css)
    )
    return [(head.strip(), body) for head, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text)]


def _of(selector: str, sheet: str, media: str | None = None) -> str:
    return "".join(
        body
        for head, body in _rules(sheet, media)
        if selector in [s.strip() for s in re.split(r",(?![^()]*\))", head)]
    )


def _token(name: str) -> str:
    return re.search(rf"{re.escape(name)}:\s*([^;]+);", (CSS / "_tokens.css").read_text())[
        1
    ].strip()


def _luminance(hex_colour: str) -> float:
    def channel(v: int) -> float:
        c = v / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hex_colour[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_a_fields_edge_stands_out_three_to_one_from_every_surface_a_field_sits_on() -> None:
    edge = _token("--field-edge")
    surfaces = {
        "#000000" if s == "#000" else s for s in map(_token, ("--bg", "--s1", "--s2", "--s3"))
    }
    assert all(_contrast(edge, s) >= 3 for s in surfaces), edge


def test_every_text_field_draws_its_hairline_as_the_edge_unless_it_is_disabled() -> None:
    rule = [
        body
        for head, body in _rules("_forms.css")
        if head.startswith(":where(input:not(")
        and head.endswith("select, textarea):not(:disabled)")
    ]
    assert rule and "--hair: var(--field-edge)" in rule[0]
    assert "1px solid var(--hair)" in _of(".inp", "_forms.css")


def test_historys_search_and_its_menus_fields_have_an_edge() -> None:
    for field in (".hx-search input", ".hx-menu__f input", ".hx-dates input"):
        assert "border: 1px solid var(--hair);" in _of(field, "_history.css"), field


def test_the_code_boxes_of_pairing_and_play_on_have_the_fields_edge() -> None:
    assert "border: 1px solid var(--field-edge)" in _of(".pair__code i", "_pair.css")
    assert "border: 1px solid var(--field-edge)" in _of(".play-on__code i", "_play_on.css")


def test_an_off_switch_shows_its_track_and_an_on_one_its_colour_alone() -> None:
    edge = _of(".tog:not(.on, :focus-visible)", "_forms.css")
    assert "outline: 1px solid var(--field-edge)" in edge and "outline-offset: -1px" in edge
    # Drawn without a border, so the switch keeps its size and the knob its place.
    assert "border:" not in _of(".tog", "_forms.css")


def test_in_the_history_pane_the_keyboard_lands_clear_of_the_tools_but_their_own_control_stays_put() -> (
    None
):
    view = '.you-view[data-view="history"]'
    # The tools' own height, as history.js measures it; before that, no less than they stand: one
    # line of pills under the search is 153px wide, and 178px on a phone, where the count wraps.
    clear = r"scroll-padding-top: calc\(var\(--hx-tools, (\d+)px\) \+ [1-9]\d*px\)"
    wide = re.search(clear, _of(view, "_history.css"))
    phone = re.search(clear, _of(view, "_history.css", "(max-width: 699.98px)"))
    assert wide and int(wide[1]) >= 153, _of(view, "_history.css")
    assert phone and int(phone[1]) >= 178
    held = view + ":has(.hx-tools:focus-within)"
    assert "scroll-padding-top: 0" in _of(held, "_history.css")


def test_on_a_wide_pane_historys_kinds_wrap_rather_than_run_past_its_edge() -> None:
    assert "flex-wrap: wrap" in _of(".hx-groups", "_history.css", "(min-width: 700px)")


def test_the_password_views_fields_keep_no_empty_room_for_a_reason_until_one_is_said() -> None:
    why = _of(".pwd--pane .pwd-why", "_password.css")
    assert "min-height: 0" in why and "padding: 0" in why
    assert "content: none" in _of(".pwd--pane .pwd-why::before", "_password.css")
    assert re.search(r"margin: 0[;\s]", _of(".pwd--pane .pwd-rules", "_password.css"))


def test_under_a_short_window_history_panes_tools_and_all_titles_head_go_with_the_list() -> None:
    short = "(max-height: 500px)"
    assert "position: relative" in _of(".hx-tools", "_history.css", short)
    assert "scroll-padding-top: 0" in _of('.you-view[data-view="history"]', "_history.css", short)
    assert "position: relative" in _of(".watch-all__head", "_watch_grid.css", short)


def test_all_titles_goes_two_across_only_where_a_poster_would_be_tiny() -> None:
    # One rule from 360 wide up, so a phone's posters match across 360-440; a very long word
    # breaks inside its card there (overflow-wrap) rather than the columns changing by width.
    two = [
        m
        for m in re.finditer(
            r"@media \(max-width: ([\d.]+)px\) \{\s*\.watch-grid-host > \.watch-grid \{ ([^}]*)\}",
            (CSS / "_watch_grid.css").read_text(),
        )
        if "repeat(2," in m[2]
    ]
    assert len(two) == 1
    first_three = float(two[0][1]) + 0.02
    assert first_three <= 360
    assert (first_three - 2 * 20 - 2 * 10) / 3 >= 100


def test_search_results_go_two_across_where_all_titles_does() -> None:
    narrow = _of(".results-page .watch-grid", "_watch_search.css", "(max-width: 359.98px)")
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in narrow


def test_ask_goes_two_across_where_the_grids_do() -> None:
    narrow = _of(".ask .watch-grid", "_watch_ask.css", "(max-width: 359.98px)")
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in narrow
    assert "389.98" not in (CSS / "_watch_ask.css").read_text()


def test_settings_fields_are_16px_on_a_coarse_pointer_at_any_width() -> None:
    rules = _of(".setg-pg .inp", "_settings_pg.css", "(pointer: coarse)")
    assert "font-size: 16px" in rules


def test_the_search_hint_shrinks_to_fit_the_narrowest_phone() -> None:
    assert "font-size: 15px" in _of(
        ".watch-search__ph", "_watch_search.css", "(max-width: 359.98px)"
    )


def test_an_integration_header_gives_up_its_mark_to_keep_every_control_in_view() -> None:
    narrow = _of(
        ".setg-pg .intg-card .intg-card-hd .glyph", "_settings_pg.css", "(max-width: 239.98px)"
    )
    assert "display: none" in narrow


def test_downloading_cards_sit_on_canvas_in_forced_colours() -> None:
    media = "(forced-colors: active)"
    assert "background: Canvas" in _of(".dw__link::before", "_watch_downloading.css", media)
    assert "forced-color-adjust" not in _of(".dw__text", "_watch_downloading.css", media)


def test_without_javascript_the_downloading_placeholder_is_not_shown() -> None:
    # Nothing fills it without JavaScript, so it would read as a load that never ends.
    hidden = _of(".dlx.is-resting", "_watch_downloading.css", "(scripting: none)")
    assert "display: none" in hidden


def test_a_long_name_that_cannot_fit_its_column_breaks_at_a_syllable_with_a_hyphen() -> None:
    # A card's title and a person's name break only where hyphens allows, so "Cartographer's"
    # or "Bartholomew" never loses its last letter to a line of its own.
    named = [
        (".watch-card__title", "_watch_grid.css"),
        (".watch-person__name", "_watch_grid.css"),
        (".ttl-cast__name", "_watch_cast.css"),
        (".ttl-cast__char", "_watch_cast.css"),
        (".cast-card__name", "_watch_cast.css"),
        (".cast-card__role", "_watch_cast.css"),
        (".cast-card__poster-name", "_watch_cast.css"),
    ]
    for selector, sheet in named:
        body = _of(selector, sheet)
        assert "hyphens: auto" in body, selector
        assert "-webkit-hyphens: auto" in body, selector
    # Only a long word is hyphenated, so "Station" is never split where it would fit whole.
    root = _of(":root", "_watch_grid.css")
    assert "hyphenate-limit-chars: 11 5 4" in root
    assert "-webkit-hyphenate-limit-before: 5" in root


def test_a_phone_s_three_across_titles_are_wide_enough_for_cartographer_s() -> None:
    grid = _of(".watch-grid-host > .watch-grid", "_watch_grid.css", "(max-width: 599.98px)")
    assert "repeat(3, minmax(0, 1fr))" in grid
    gap = int(re.search(r"gap: \d+px (\d+)px", grid).group(1))
    pad = int(re.search(r"--pad:\s*max\(clamp\((\d+)px,\s*4vw", _token_sheet()).group(1))
    # At 360 the page's 4vw is under the clamp's floor, so the floor is the side padding.
    assert pad > 0.04 * 360
    column = (360 - 2 * pad - 2 * gap) / 3
    size = int(
        re.search(
            r"font-size: (\d+)px", _of(".watch-card__title", "_watch.css", "(max-width: 599.98px)")
        ).group(1)
    )
    # "Cartographer" in bold is about 6.3 times the font size wide.
    assert column >= 6.3 * size + 18


def _token_sheet() -> str:
    return (CSS / "_tokens.css").read_text(encoding="utf-8")


def test_on_a_low_wide_pane_historys_kinds_stay_on_one_line_and_scroll_sideways() -> None:
    low = "(max-height: 500px) and (min-width: 700px)"
    assert "flex-wrap: nowrap" in _of(".hx-groups", "_history.css", low)
