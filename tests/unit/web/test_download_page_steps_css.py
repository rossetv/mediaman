"""The mailed page's steps: labels keep a gap and a phone's row holds two lines."""

from __future__ import annotations

import re
from pathlib import Path

CSS = (
    Path(__file__).resolve().parents[3] / "src/mediaman/web/static/css/_download_page.css"
).read_text("utf-8")


def _rule(selector: str) -> str:
    m = re.search(re.escape(selector) + r" \{([^}]*)\}", CSS)
    assert m is not None, selector
    return m[1]


def test_a_step_label_keeps_a_gap_before_the_next_step() -> None:
    assert "padding-right" in _rule(".share-steps li:not(:last-child)")


def test_a_phones_step_row_holds_two_lines_so_a_longer_label_does_not_move_the_page() -> None:
    phone = CSS.split("@media (max-width: 699.98px) {", 1)[1]
    m = re.search(r"\.share-steps span \{([^}]*)\}", phone)
    assert m is not None and "min-height: 2lh" in m[1]
