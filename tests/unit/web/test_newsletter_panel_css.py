"""The newsletter panel's notes and buttons hold up in forced colours and at 200% text.

These read the stylesheet rules that carry that, since no layout engine runs here.
"""

from __future__ import annotations

from tests.unit.web._css import px, style

PANELS = "_settings_pg_panels.css"


def test_an_error_note_keeps_a_cue_in_forced_colours() -> None:
    note = style(".setg-pg .fld-sub.is-err", PANELS, "(forced-colors: active)")
    width, line, _ = note["border-inline-start"].split()
    assert px(width) >= 1 and line == "solid"


def test_the_select_buttons_wrap_rather_than_clip_in_a_narrow_box() -> None:
    row = style(".setg-pg .recipient-list .recipient-toggles", PANELS)
    button = style(".setg-pg .recipient-toggles .btn", PANELS)
    assert row["flex-wrap"] == "wrap"
    assert button["white-space"] == "normal" and button["max-width"] == "100%"
