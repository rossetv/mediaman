"""Settings and Downloads let their boxes shrink, so a very narrow screen does not scroll sideways.

These read the stylesheet rules that carry that, since no layout engine runs here.
"""

from __future__ import annotations

import re

from tests.unit.web._css import CSS_DIR, rules, style

PANELS = "_settings_pg_panels.css"


def _tracks(value: str) -> list[str]:
    return re.findall(r"[\w-]+\([^)]*\)|\S+", value)


def test_the_overview_cards_are_one_full_width_column_that_may_shrink_below_their_words() -> None:
    (track,) = _tracks(style(".setg-pg .ov-hero", PANELS)["grid-template-columns"])
    assert track.replace(" ", "").startswith("minmax(0,")
    assert style(".setg-pg .ov-card", PANELS)["min-width"] == "0"
    # no screen width lays them out otherwise
    assert not [
        where
        for where, selectors, decls in rules(PANELS)
        if where and any(".ov-hero" in s for s in selectors) and "grid-template-columns" in decls
    ]


def test_storage_comes_before_the_next_scan_in_the_overview() -> None:
    page = (CSS_DIR.parents[1] / "templates" / "settings" / "_sec_overview.html").read_text()
    assert 0 <= page.index('class="ov-card ov-storage"') < page.index('class="ov-card ov-scan"')


def test_an_overview_figure_wraps_its_words_whole_onto_an_unindented_line() -> None:
    big = style(".setg-pg .ov-card .big", PANELS)
    assert big["flex-wrap"] == "wrap" and "column-gap" in big
    small = style(".setg-pg .ov-card .big small", PANELS)
    assert small["white-space"] == "nowrap"
    assert not {"margin", "margin-left", "margin-inline", "margin-inline-start"} & set(small)


def test_a_download_row_lets_its_title_column_shrink() -> None:
    _, title, *_ = _tracks(style(".dl-row", "_dl.css")["grid-template-columns"])
    assert title.replace(" ", "").startswith("minmax(0,")


def test_a_long_recipient_address_wraps_instead_of_being_cut() -> None:
    label = style(".setg-pg .recipient-item label", PANELS)
    assert label["overflow-wrap"] == "anywhere"
    assert label.get("white-space") != "nowrap" and "text-overflow" not in label
