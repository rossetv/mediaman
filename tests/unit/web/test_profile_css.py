"""The Profile pane's stylesheet (_profile.css): how its hub and views feel under a finger and a pointer."""

from __future__ import annotations

from tests.unit.web._css import rules, style


def test_a_hover_lights_a_row_only_where_there_is_a_pointer_to_hover_with() -> None:
    # A tap on a phone leaves :hover on, so a hover rule outside the media query would stay lit.
    outside = [s for media, sels, _ in rules("_profile.css") if media == () for s in sels]
    assert not [s for s in outside if ":hover" in s]
    lit = [
        s for media, sels, _ in rules("_profile.css") if media == ("(hover: hover)",) for s in sels
    ]
    assert any(".yo-row:hover" in s for s in lit) and any(".yo-tile:hover" in s for s in lit)


def test_on_a_short_screen_the_save_button_follows_the_form_and_covers_nothing() -> None:
    assert style(".yo-foot", "_profile.css", "(max-height: 500px)")["position"] == "static"
