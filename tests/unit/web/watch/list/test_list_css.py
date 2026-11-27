"""The stylesheets of All titles and of the dialogs its admin actions open.

Each check reads the rule a phone or a narrow window actually gets, so a target
that shrinks or a breakpoint that moves back fails here.
"""

from __future__ import annotations

from tests.unit.web._css import px, style

TOUCH = "(pointer: coarse), (max-width: 699.98px)"


def test_the_dialogs_controls_are_44px_by_touch() -> None:
    for selector in (".keep-dur-btn", ".keep-dialog .btn--sm"):
        assert style(selector, "_modal.css", TOUCH)["min-height"] == "44px", selector


def test_a_season_to_delete_is_waited_for_at_the_height_of_its_row() -> None:
    row = style("#season-delete-list > .keep-season-item", "_modal.css")
    skeleton = style("#season-delete-list > .keep-season-item--skel", "_modal.css")
    assert skeleton["height"] == row["min-height"]


def test_a_kept_season_s_button_has_room_round_it_by_touch_on_a_tablet() -> None:
    room = px(style("#keep-season-list > .keep-season-item", "_modal.css")["padding-block"])
    held = px(
        style("#keep-season-list.has-keeps > .keep-season-item", "_modal.css", TOUCH)["min-height"]
    )
    assert room >= 4
    # The keep, 22px and 4px, is over it.
    assert held >= 22 + 4 + 44 + 2 * room


def test_the_durations_stay_on_one_line_on_the_narrowest_phones() -> None:
    narrow = "(max-width: 389.98px)"
    assert style(".keep-duration-bar", "_modal.css", narrow)["flex-wrap"] == "nowrap"
    assert style(".keep-dur-btn", "_modal.css", narrow)["white-space"] == "nowrap"


def test_on_a_short_screen_the_dialog_scrolls_as_a_whole_and_not_a_list_squeezed_inside_it() -> (
    None
):
    short = "(max-height: 400px)"
    assert style(".keep-dialog", "_modal.css")["overflow-y"] == "auto"
    inner = style(".keep-season-list", "_modal.css", short)
    assert (inner["flex-shrink"], inner["max-height"], inner["overflow"]) == (
        "0",
        "none",
        "visible",
    )


def test_the_filters_of_all_titles_are_44px_on_a_phone_and_reached_from_44px_elsewhere() -> None:
    pill = style(".watch-all__filters > .fpill", "_watch_grid.css")
    phone = style(".watch-all__filters > .fpill", "_watch_grid.css", "(max-width: 699.98px)")
    assert phone["min-height"] == "44px"
    # Elsewhere the pill's own height plus its reach, above and below, makes 44px.
    reach = -px(style(".watch-all__filters > .fpill::after", "_watch_grid.css")["inset"].split()[0])
    assert px(pill["min-height"]) + 2 * reach >= 44
    # The row clips what it scrolls: the reach must be within its own padding.
    padding = style(".watch-all__filters", "_watch_grid.css")["padding"].split()
    assert px(padding[0]) >= reach
