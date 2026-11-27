"""Settings and Downloads let their boxes shrink, so a very narrow screen does not scroll sideways.

These read the stylesheet rules that carry that, since no layout engine runs here.
"""

from __future__ import annotations

import re
from pathlib import Path

CSS = Path(__file__).parents[3] / "src" / "mediaman" / "web" / "static" / "css"


def _sheet(name: str) -> str:
    return re.sub(r"/\*.*?\*/", "", (CSS / name).read_text(), flags=re.S)


def _declarations(selector: str, css: str) -> str:
    blocks = re.findall(r"([^{}]+)\{([^{}]*)\}", css)
    return " ".join(
        body for head, body in blocks if any(part.strip() == selector for part in head.split(","))
    )


def test_the_overview_cards_are_one_full_width_column_that_may_shrink_below_their_words() -> None:
    panels = _sheet("_settings_pg_panels.css")
    assert "grid-template-columns: minmax(0, 1fr)" in _declarations(".setg-pg .ov-hero", panels)
    assert "min-width: 0" in _declarations(".setg-pg .ov-card", panels)
    assert ".ov-hero" not in "".join(re.findall(r"@media[^{]*\{[^@]*", panels))


def test_storage_comes_before_the_next_scan_in_the_overview() -> None:
    page = (CSS.parents[1] / "templates" / "settings" / "_sec_overview.html").read_text()
    assert 0 <= page.index('class="ov-card ov-storage"') < page.index('class="ov-card ov-scan"')


def test_an_overview_figure_wraps_its_words_whole_onto_an_unindented_line() -> None:
    panels = _sheet("_settings_pg_panels.css")
    big = _declarations(".setg-pg .ov-card .big", panels)
    assert "flex-wrap: wrap" in big and "column-gap" in big
    small = _declarations(".setg-pg .ov-card .big small", panels)
    assert "white-space: nowrap" in small
    assert "margin-left" not in small


def test_a_download_row_lets_its_title_column_shrink() -> None:
    row = _declarations(".dl-row", _sheet("_dl.css"))
    assert "48px minmax(0, 1fr) auto" in row


def test_a_long_recipient_address_wraps_instead_of_being_cut() -> None:
    label = _declarations(".setg-pg .recipient-item label", _sheet("_settings_pg_panels.css"))
    assert "overflow-wrap: anywhere" in label
    assert "ellipsis" not in label and "nowrap" not in label
