"""The list view's stylesheet: 44px targets below 700px and the card layout up to there.

Each check reads the rule a phone or a narrow window actually gets, so a target
that shrinks or a breakpoint that moves back fails here.
"""

from __future__ import annotations

import re
from pathlib import Path

CSS_DIR = Path(__file__).parents[5] / "src" / "mediaman" / "web" / "static" / "css"
CSS = (CSS_DIR / "_watch_list.css").read_text()
MODAL_CSS = (CSS_DIR / "_modal.css").read_text()


def _block(query: str, css: str = CSS) -> str:
    """The body of the first @media block whose query is *query*."""
    start = css.index("@media " + query + " {")
    depth = 0
    for i in range(start, len(css)):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                return css[start:i]
    raise AssertionError(query)


def test_the_small_row_actions_take_44px_below_700_even_with_a_mouse():
    block = _block("(pointer: coarse), (max-width: 699.98px)")
    assert re.search(
        r"\.wl-table \.keep-trigger,\s*\.wl-table \.btn--sm \{ height: 44px; min-height: 44px; \}",
        block,
    )


def test_rows_are_cards_up_to_700_so_the_title_is_never_squeezed():
    block = _block("(max-width: 699.98px)")
    assert ".wl-table thead { display: none; }" in block
    assert "display: grid;" in block
    assert "@media (max-width: 639.98px)" not in CSS


def test_the_sort_select_the_pills_and_the_title_are_44px_targets_below_700():
    block = _block("(max-width: 699.98px)")
    assert ".wl-sort .inp { flex: 1; min-height: 44px; }" in block
    assert re.search(r"\.wl-toolbar \.fpill \{[^}]*min-width: 44px", block)
    assert ".wl-title { position: relative; }" in block
    assert re.search(r"a\.wl-title::after \{[^}]*position: absolute; inset: -14px -6px;", block)


def test_the_tablet_actions_column_takes_only_the_width_it_needs():
    block = _block("(min-width: 700px) and (max-width: 1023.98px)")
    assert ".wl-table .col-act { width: auto; }" in block


def test_a_phone_says_when_a_title_was_last_watched_as_a_tablet_does():
    assert ".wl-sub-watched { display: inline; }" in _block("(max-width: 1023.98px)")


def test_the_empty_list_s_buttons_stand_the_group_s_gap_apart_and_no_more():
    assert not re.search(r"\.wl-empty \.btn \{[^}]*margin", CSS)
    assert re.search(
        r"\.wl-empty > \.btn,\s*\.wl-empty \.watch-actions \{ margin-top: 18px; \}", CSS
    )


def test_a_sortable_header_starts_where_its_column_does_on_a_tablet():
    block = _block("(max-width: 1023.98px)")
    assert re.search(
        r"thead th:not\(\.sortable\),\s*\.wl-table thead th\.sortable \.th-sort,", block
    )


def test_the_skeleton_has_the_sort_s_own_bar_on_a_phone():
    assert re.search(r"\.wl-skel__bar--sort \{ display: block;", _block("(max-width: 699.98px)"))


def test_the_dialogs_controls_are_44px_by_touch_and_the_season_deletes_stand_apart():
    block = _block("(pointer: coarse), (max-width: 699.98px)", MODAL_CSS)
    assert re.search(r"\.keep-dur-btn,[^}]*\.keep-dialog \.btn--sm \{ min-height: 44px; \}", block)
    assert ".keep-season-list { gap: 8px; }" in block
    assert "#season-delete-list > .keep-season-item { padding-block: 4px; }" in block


def test_a_season_to_delete_is_waited_for_at_the_height_of_its_row():
    waiting = r"#season-delete-list > \.keep-season-item--skel \{ height: (\d+)px; \}"
    assert re.findall(waiting, MODAL_CSS) == ["52"]


def test_a_season_to_delete_has_its_details_below_its_name_on_a_phone_and_room_for_two_lines():
    block = _block("(max-width: 639.98px)", MODAL_CSS)
    assert re.search(
        r"#season-delete-list > \.keep-season-item \{ min-height: 68px; padding-block: 6px;", block
    )
    assert re.search(
        r":not\(\.keep-season-item--skel\) \{\s*display: grid;\s*"
        r"grid-template-columns: minmax\(0, 1fr\) auto;",
        block,
    )
    assert re.search(
        r"#season-delete-list \.keep-season-details \{ grid-area: 2 / 1; margin-left: 0;", block
    )


def test_the_dialogs_leave_the_narrowest_phones_room_for_the_details():
    assert ".keep-dialog { padding-inline: 16px; }" in _block("(max-width: 359.98px)", MODAL_CSS)


def test_a_kept_season_s_button_has_room_round_it_by_touch_on_a_tablet():
    block = _block("(pointer: coarse) and (min-width: 700px)", MODAL_CSS)
    assert ".keep-season-item--kept { padding-block: 4px; }" in block


def test_the_durations_stay_on_one_line_on_the_narrowest_phones():
    block = _block("(max-width: 389.98px)", MODAL_CSS)
    assert re.search(r"\.keep-duration-bar \{[^}]*flex-wrap: nowrap;", block)
    assert re.search(r"\.keep-dur-btn \{[^}]*white-space: nowrap;", block)


def test_the_filter_pills_reach_44px_by_touch_on_a_tablet():
    block = _block("(pointer: coarse) and (min-width: 700px)")
    assert re.search(r"\.fpill::after \{[^}]*inset: calc\(\(100% - 44px\) / 2\)", block)


def test_a_kept_show_s_dialog_fits_a_narrow_phone_with_remove_all_on_its_own_line():
    block = _block("(max-width: 389.98px)", MODAL_CSS)
    assert ".keep-dialog-actions { flex-wrap: wrap; }" in block
    assert "#keep-dialog-remove.is-visible { flex: 1 0 100%; }" in block
    assert re.search(r"\.keep-season-keep \{ flex-wrap: wrap;", block)


def test_the_season_list_does_not_reach_past_the_dialog_s_own_padding():
    (rule,) = re.findall(r"\n\.keep-season-list \{([^}]*)\}", MODAL_CSS)
    assert "margin" not in rule
    assert re.search(
        r"\.keep-season-note,\s*\.keep-season-list > \.btn \{ margin-inline: 4px; \}", MODAL_CSS
    )


def test_a_kept_season_in_the_delete_list_is_as_tall_as_one_with_a_button():
    assert "\n#season-delete-list > .keep-season-item { min-height: 52px; }" in MODAL_CSS


def test_details_of_several_lines_do_not_touch_their_row_s_edge_on_a_phone():
    block = _block("(max-width: 639.98px)", MODAL_CSS)
    assert re.search(r"\n  \.keep-season-details \{[^}]*padding-block: 4px;", block)
