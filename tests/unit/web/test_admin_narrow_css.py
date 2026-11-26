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


def test_the_overview_cards_may_be_narrower_than_their_words() -> None:
    panels = _sheet("_settings_pg_panels.css")
    assert "minmax(0, 1.55fr)" in _declarations(".setg-pg .ov-hero", panels)
    assert "min-width: 0" in _declarations(".setg-pg .ov-card", panels)
    assert "grid-template-columns: minmax(0, 1fr)" in panels


def test_a_download_row_lets_its_title_column_shrink() -> None:
    row = _declarations(".dl-row", _sheet("_dl.css"))
    assert "48px minmax(0, 1fr) auto" in row


def test_a_long_recipient_address_wraps_instead_of_being_cut() -> None:
    label = _declarations(".setg-pg .recipient-item label", _sheet("_settings_pg_panels.css"))
    assert "overflow-wrap: anywhere" in label
    assert "ellipsis" not in label and "nowrap" not in label
