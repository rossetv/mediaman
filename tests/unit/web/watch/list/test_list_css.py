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


def _block(query: str) -> str:
    """The body of the first @media block whose query is *query*."""
    start = CSS.index("@media " + query + " {")
    depth = 0
    for i in range(start, len(CSS)):
        if CSS[i] == "{":
            depth += 1
        elif CSS[i] == "}":
            depth -= 1
            if depth == 0:
                return CSS[start:i]
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


def test_the_season_deletes_are_apart_on_a_phone():
    phone = MODAL_CSS.split("@media (max-width: 699.98px) {", 1)[1]
    assert ".keep-season-list { gap: 8px; }" in phone.split("@media", 1)[0]


def test_a_season_to_delete_is_waited_for_at_its_button_s_height_from_700_up():
    wide = MODAL_CSS.split("@media (min-width: 700px) {", 1)[1]
    rule = "#season-delete-list > .keep-season-item--skel { height: 52px; }"
    assert rule in wide.split("@media", 1)[0]
