"""A field's edge, the bars the keyboard must stay clear of, History's row of kinds, and the
grids' columns on a phone, read as rules."""

from __future__ import annotations

import re

from tests.unit.web._css import contrast, medias, px, rgb, rules, style, token

HISTORY = '.you-view[data-view="history"]'


def test_a_fields_edge_stands_out_three_to_one_from_every_surface_a_field_sits_on() -> None:
    edge = rgb(token("--field-edge"))
    for name in ("--bg", "--s1", "--s2", "--s3"):
        assert contrast(edge, rgb(token(name))) >= 3, name


def test_every_text_field_draws_its_hairline_as_the_edge_unless_it_is_disabled() -> None:
    drawn = [
        decls
        for _, selectors, decls in rules("_forms.css")
        if any(
            s.startswith(":where(input:not(") and s.endswith("textarea):not(:disabled)")
            for s in selectors
        )
    ]
    assert drawn and drawn[0].get("--hair") == "var(--field-edge)"
    assert "var(--hair)" in style(".inp", "_forms.css")["border"]


def test_fields_that_draw_their_own_border_draw_it_in_the_field_s_edge() -> None:
    # On an input --hair is the edge (the rule above); on any other box only the token itself is.
    own = {
        (".hx-search input", "_history.css"): {"var(--hair)", "var(--field-edge)"},
        (".hx-menu__f input", "_history.css"): {"var(--hair)", "var(--field-edge)"},
        (".hx-dates input", "_history.css"): {"var(--hair)", "var(--field-edge)"},
        (".pair__code i", "_pair.css"): {"var(--field-edge)"},
        (".play-on__code i", "_play_on.css"): {"var(--field-edge)"},
    }
    for (selector, sheet), edges in own.items():
        width, line, colour = style(selector, sheet)["border"].split()
        assert px(width) >= 1 and line == "solid" and colour in edges, selector


def test_an_off_switch_shows_its_track_and_an_on_one_its_colour_alone() -> None:
    off = style(".tog:not(.on, :focus-visible)", "_forms.css")
    assert "var(--field-edge)" in off["outline"] and off["outline-offset"] == "-1px"
    # A border would change the switch's size and move its knob.
    assert "border" not in style(".tog", "_forms.css")


def test_in_the_history_pane_the_keyboard_lands_clear_of_the_tools_but_their_own_control_stays_put() -> (
    None
):
    clear = r"calc\(var\(--hx-tools, (\d+)px\) \+ [1-9]\d*px\)"
    wide = re.fullmatch(clear, style(HISTORY, "_history.css")["scroll-padding-top"])
    phone = re.fullmatch(
        clear, style(HISTORY, "_history.css", "(max-width: 699.98px)")["scroll-padding-top"]
    )
    # One line of pills under the search is 153px tall, and 178px on a phone, where the count wraps.
    assert wide and int(wide[1]) >= 153
    assert phone and int(phone[1]) >= 178
    held = style(HISTORY + ":has(.hx-tools:focus-within)", "_history.css")
    assert held["scroll-padding-top"] == "0"


def test_historys_kinds_keep_one_line_that_scrolls_sideways_at_every_pane_height() -> None:
    assert style(".hx-groups", "_history.css")["overflow-x"] == "auto"
    for media in (None, "(min-width: 700px)", "(max-height: 500px) and (min-width: 700px)"):
        assert style(".hx-groups", "_history.css", media).get("flex-wrap") != "wrap", media


def test_under_a_short_window_history_panes_tools_and_all_titles_head_go_with_the_list() -> None:
    short = "(max-height: 500px)"
    # The pane is a sheet over the whole width below 700px, which has no room to keep the tools.
    short_and_wide = f"{short} and (min-width: 700px)"
    assert style(".hx-tools", "_history.css", short_and_wide)["position"] == "relative"
    assert style(HISTORY, "_history.css", short_and_wide)["scroll-padding-top"] == "0"
    assert style(".watch-all__head", "_watch_grid.css", short)["position"] == "relative"


def test_all_titles_goes_two_across_only_where_a_poster_would_be_tiny() -> None:
    ((query,),) = medias(
        ".watch-grid-host > .watch-grid", "_watch_grid.css", "grid-template-columns", "repeat(2,"
    )
    first_three = float(re.search(r"max-width: ([\d.]+)px", query)[1]) + 0.02
    assert first_three <= 360
    # Three across on the narrowest phone still leaves a poster of at least 100px.
    assert (first_three - 2 * 20 - 2 * 10) / 3 >= 100


def test_search_results_and_ask_go_two_across_at_the_width_all_titles_does() -> None:
    columns = "grid-template-columns"
    titles = medias(".watch-grid-host > .watch-grid", "_watch_grid.css", columns, "repeat(2,")
    assert medias(".results-page .watch-grid", "_watch_search.css", columns, "repeat(2,") == titles
    assert medias(".ask .watch-grid", "_watch_ask.css", columns, "repeat(2,") == titles


def test_settings_fields_are_16px_on_a_coarse_pointer_at_any_width() -> None:
    # Below 16px a phone zooms the page when a field takes focus.
    assert style(".setg-pg .inp", "_settings_pg.css", "(pointer: coarse)")["font-size"] == "16px"


def test_downloading_cards_sit_on_canvas_in_forced_colours() -> None:
    media = "(forced-colors: active)"
    assert style(".dw__link::before", "_watch_downloading.css", media)["background"] == "Canvas"
    assert "forced-color-adjust" not in style(".dw__text", "_watch_downloading.css", media)


def test_a_downloading_cards_long_name_takes_two_lines_and_is_never_cut_on_one() -> None:
    title = style(".dw__title", "_watch_downloading.css")
    assert title.get("-webkit-line-clamp") == title.get("line-clamp") == "2"
    assert title.get("display") == "-webkit-box"
    # One line with an ellipsis would cut the name mid-word, at any width.
    sheet = rules("_watch_downloading.css")
    assert not [d for _, sels, d in sheet if ".dw__title" in sels and "white-space" in d]


def test_without_javascript_the_downloading_placeholder_is_not_shown() -> None:
    # Nothing fills it without JavaScript, so it would read as a load that never ends.
    hidden = style(".dlx.is-resting", "_watch_downloading.css", "(scripting: none)")
    assert hidden["display"] == "none"


def test_a_long_name_that_cannot_fit_its_column_breaks_at_a_syllable_with_a_hyphen() -> None:
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
        body = style(selector, sheet)
        # Safari needs the prefixed form.
        assert body.get("hyphens") == "auto", selector
        assert body.get("-webkit-hyphens") == "auto", selector


def test_a_phone_s_three_across_titles_are_wide_enough_for_cartographer_s() -> None:
    narrow = "(max-width: 599.98px)"
    grid = style(".watch-grid-host > .watch-grid", "_watch_grid.css", narrow)
    assert grid["grid-template-columns"] == "repeat(3, minmax(0, 1fr))"
    gap = px(grid["gap"].split()[1])
    pad = int(re.search(r"max\(clamp\((\d+)px,\s*4vw", token("--pad"))[1])
    # At 360 the page's 4vw is under the clamp's floor, so the floor is the side padding.
    assert pad > 0.04 * 360
    column = (360 - 2 * pad - 2 * gap) / 3
    size = px(style(".watch-card__title", "_watch.css", narrow)["font-size"])
    # "Cartographer" in bold is about 6.3 times the font size wide.
    assert column >= 6.3 * size + 18
