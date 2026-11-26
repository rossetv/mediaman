"""The newsletter panel's notes and buttons hold up in forced colours and at 200% text.

These read the stylesheet rules that carry that, since no layout engine runs here.
"""

from __future__ import annotations

import re
from pathlib import Path

CSS = Path(__file__).parents[3] / "src" / "mediaman" / "web" / "static" / "css"
PANELS = re.sub(r"/\*.*?\*/", "", (CSS / "_settings_pg_panels.css").read_text(), flags=re.S)
FORCED = " ".join(re.findall(r"@media \(forced-colors: active\) \{(.*?)\n\}", PANELS, flags=re.S))


def _declarations(selector: str, css: str) -> str:
    blocks = re.findall(r"([^{}]+)\{([^{}]*)\}", css)
    return " ".join(
        body for head, body in blocks if any(part.strip() == selector for part in head.split(","))
    )


def test_an_error_note_keeps_a_cue_in_forced_colours() -> None:
    cue = _declarations(".setg-pg .fld-sub.is-err", FORCED)
    assert re.search(r"border-inline-start:\s*\d+px solid CanvasText", cue)


def test_the_select_buttons_wrap_rather_than_clip_in_a_narrow_box() -> None:
    row = _declarations(".setg-pg .recipient-list .recipient-toggles", PANELS)
    button = _declarations(".setg-pg .recipient-toggles .btn", PANELS)
    assert "flex-wrap: wrap" in row
    assert "white-space: normal" in button and "max-width: 100%" in button
