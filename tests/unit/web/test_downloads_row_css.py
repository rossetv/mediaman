"""The /downloads queue rows' layout around a row's abandon button, read from _dl.css."""

from __future__ import annotations

import re

from tests.unit.web._css import px, rules, style

SHEET = "_dl.css"
ROW = ".dl-compact > .dl-row"
WITH_BUTTON = ".dl-compact > .dl-row--has-abandon"
WIDE = "(min-width: 375px)"
ROOMY = "(min-width: 640px)"
NARROW = "(max-width: 374.98px)"
PHONE = "(max-width: 639.98px)"
TINY = "(max-width: 259.98px)"
TOUCH = "(pointer: coarse)"


def _tracks(columns: str | None) -> int:
    assert columns, "no grid-template-columns"
    return len(re.findall(r"minmax\([^)]*\)|calc\([^)]*\)|var\([^)]*\)|[^\s()]+", columns))


def _columns(selector: str, media: str | None = None) -> str | None:
    return style(selector, SHEET, media).get("grid-template-columns")


def _area(selector: str, media: str) -> list[str]:
    """The grid-area of *selector* in *media* as its row-start, column-start, row-end, column-end."""
    return [part.strip() for part in style(selector, SHEET, media)["grid-area"].split("/")]


def test_a_rows_abandon_button_has_a_column_of_its_own_at_the_rows_end() -> None:
    plain = _columns(".dl-compact-row")
    assert _tracks(_columns(WITH_BUTTON, WIDE)) == _tracks(plain) + 1
    button = style(".dl-compact .dl-row-abandon", SHEET)
    assert button["grid-area"] == "1 / -2"
    # the column and the row padding the button reaches into hold the button, at both sizes
    reach = -px(button["margin-right"])
    for media, icon in (
        (None, style(".btn--icon", "_buttons.css")),
        (TOUCH, style(".dl-row-abandon .btn.btn--icon", SHEET, TOUCH)),
    ):
        column = px(style(".dl-compact-list", SHEET, media)["--dl-abandon-col"])
        assert column + reach == px(icon["width"]), media


def test_rows_without_the_button_keep_its_column_so_percentages_line_up_down_the_list() -> None:
    aligned = [
        sel
        for where, sels, _ in rules(SHEET)
        if where == (ROOMY,)
        for sel in sels
        if ":has(" in sel and ".dl-row--has-abandon" in sel
    ]
    assert aligned, "no rule for the rows beside one with the button"
    assert all(sel.endswith("> .dl-compact > .dl-row") for sel in aligned), aligned
    columns = {_columns(sel, ROOMY) for sel in aligned}
    columns.add(_columns(WITH_BUTTON, WIDE))
    assert len(columns) == 1


def test_on_a_narrow_phone_the_button_is_the_only_extra_column() -> None:
    assert _columns(WITH_BUTTON, NARROW) is None
    assert _tracks(_columns(ROW, NARROW)) == 3
    # the percentage and time left go under the title, out of the button's column
    right = _area(".dl-compact > .dl-row--has-abandon .dl-compact-right", NARROW)
    assert right[1] == "2"


def test_on_a_phone_an_episodes_name_has_the_mini_bars_room() -> None:
    assert style(".dl-ep-mini-bar", SHEET, PHONE)["display"] == "none"
    assert style(".dl-ep-status-pill", SHEET, PHONE)["grid-column"] == "3 / -1"


def test_upcoming_row_gives_the_title_a_full_line_below_260px() -> None:
    """With the poster and the button sharing a line, the title keeps the row's width and the
    button is no longer laid over the pill."""
    poster = _area(".dl-upcoming-row > .dl-row-poster", TINY)
    button = _area(".dl-upcoming-row > .dl-row-abandon", TINY)
    info = _area(".dl-upcoming-row > .dl-row-info", TINY)
    pill = _area(".dl-upcoming-row > .pill", TINY)
    assert button[0] == poster[0], "the button shares the poster's line"
    assert int(info[0]) > int(poster[0]) and int(pill[0]) > int(info[0])
    assert int(info[2]) <= int(pill[0]), "the title ends above the pill"
    assert info[1::2] == pill[1::2] == ["1", "-1"], "the title and the pill span the row"
    assert style(".dl-upcoming-row > .dl-row-abandon", SHEET, TINY)["position"] == "static"
    # The row's right padding no longer reserves the button's column.
    tiny_row = style(".dl-upcoming-row.dl-row--has-abandon", SHEET, TINY)
    assert "padding-right" not in tiny_row and len(set(tiny_row["padding"].split())) == 1
    assert style(".dl-upcoming-row .dl-row-title", SHEET, TINY)["white-space"] == "normal"
