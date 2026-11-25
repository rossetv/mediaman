"""The stylesheets of All titles and of the dialogs its admin actions open.

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
    (rule,) = re.findall(r"\n#keep-season-list > \.keep-season-item \{([^}]*)\}", MODAL_CSS)
    (room,) = (int(n) for n in re.findall(r"padding-block: (\d+)px", rule))
    touch = next(
        block
        for block in MODAL_CSS.split("@media (pointer: coarse), (max-width: 699.98px)")[1:]
        if "has-keeps" in block
    )
    held = re.search(r"has-keeps > \.keep-season-item \{ min-height: (\d+)px", touch)
    assert room >= 4
    # the keep, 22px and 4px, is over it
    assert held and int(held[1]) >= 22 + 4 + 44 + 2 * room


def test_the_durations_stay_on_one_line_on_the_narrowest_phones():
    block = _block("(max-width: 389.98px)", MODAL_CSS)
    assert re.search(r"\.keep-duration-bar \{[^}]*flex-wrap: nowrap;", block)
    assert re.search(r"\.keep-dur-btn \{[^}]*white-space: nowrap;", block)


def test_a_kept_show_s_dialog_fits_a_narrow_phone_with_remove_all_beside_the_others():
    block = _block("(max-width: 389.98px)", MODAL_CSS)
    assert ".keep-dialog-actions .btn { padding-inline: 8px; white-space: nowrap; }" in block
    # It gives way first, so "Keeping…" has room.
    assert "#keep-dialog-remove.btn { flex: 0 1 auto; min-width: 0; white-space: normal;" in block
    assert ".keep-season-keep .pill { grid-area: 3 / 2 / auto / 4;" in MODAL_CSS
    assert ".keep-season-keep .btn { grid-area: 1 / 3; }" in MODAL_CSS


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


def test_on_a_short_screen_the_dialog_scrolls_as_a_whole_and_not_a_list_squeezed_inside_it():
    block = _block("(max-height: 400px)", MODAL_CSS)
    (rule,) = re.findall(r"\.keep-season-list \{([^}]*)\}", block)
    for said in ("flex-shrink: 0;", "max-height: none;", "overflow: visible;"):
        assert said in rule
    # What gets the focus is brought in with its ring.
    assert re.search(r"\.keep-dialog \{[^}]*scroll-padding-block: 8px;", block)
    assert re.search(r"\n\.keep-dialog \{[^}]*overflow-y: auto;", MODAL_CSS)


def test_the_filters_of_all_titles_are_44px_on_a_phone_and_reached_from_44px_elsewhere():
    grid = (CSS_DIR / "_watch_grid.css").read_text()
    phones = [
        b for b in re.split(r"(?=@media)", grid) if b.startswith("@media (max-width: 699.98px)")
    ]
    assert any(".watch-all__filters > .fpill { min-height: 44px; }" in b for b in phones)
    pill = re.search(r"\.watch-all__filters > \.fpill \{([^}]*)\}", grid)
    reach = re.search(r"\.watch-all__filters > \.fpill::after \{([^}]*)\}", grid)
    row = re.search(r"\.watch-all__filters \{([^}]*)\}", grid)
    assert pill and "min-height: 36px" in pill[1]
    assert reach and "inset: -4px 0" in reach[1]
    # The row clips what it scrolls: the reach must be within its own padding.
    assert row and "padding: 4px var(--pad) 6px" in row[1]
