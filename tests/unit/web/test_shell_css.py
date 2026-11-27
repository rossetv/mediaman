"""The shared look of buttons and the sign-in pages: colours that keep their contrast, and sizes that fit a finger."""

from __future__ import annotations

import re

from tests.unit.web._css import contrast, rgb, rules, style, token

WHITE = (255.0, 255.0, 255.0)


def _var(body: dict[str, str], prop: str) -> str:
    return re.fullmatch(r"var\((--[\w-]+)\)", body[prop])[1]  # type: ignore[index]


def _on(tint: str, surface: str) -> tuple[float, ...]:
    """The colour of the tint token laid over the surface token."""
    *channels, alpha = rgb(token(tint))
    return tuple(
        c * alpha + u * (1 - alpha) for c, u in zip(channels, rgb(token(surface)), strict=True)
    )


def test_white_text_keeps_aa_on_a_primary_button_under_the_pointer() -> None:
    for sheet, button in (("_buttons.css", ".btn--primary"), ("_login.css", ".login-btn")):
        for state in ("", ":hover"):
            fill = rgb(token(_var(style(button + state, sheet), "background")))
            assert contrast(WHITE, fill) >= 4.5, button + state


def test_blue_and_purple_text_keep_aa_on_their_tints_over_every_surface_a_pill_sits_on() -> None:
    wearers = {
        ".pill--tv": "--accent-text",
        ".pill--anime": "--purple-text",
        ".btn--accent-soft": "--accent-text",
    }
    for selector, colour in wearers.items():
        body = style(selector, "_buttons.css")
        assert _var(body, "color") == colour, selector
        for surface in ("--s1", "--s2", "--s3", "--s4"):
            ratio = contrast(rgb(token(colour)), _on(_var(body, "background"), surface))
            assert ratio >= 4.5, f"{selector} over {surface}"


def test_a_button_is_reached_from_44px_on_tablets_and_touch_screens_without_growing() -> None:
    query = "(min-width: 700px) and (max-width: 1023.98px), (pointer: coarse)"
    # Half of what the button lacks to 44px, above and below, whatever its size.
    assert "(100% - 44px) / 2" in style(".btn::after", "_buttons.css", query)["inset"]
    assert "min-height" not in style(".btn", "_buttons.css", query)


def test_each_pane_view_ends_with_room_under_its_last_card_and_the_home_indicator() -> None:
    padding = style(".you-view", "_you.css")["padding"]
    room = re.search(r"calc\((\d+)px \+ env\(safe-area-inset-bottom, 0px\)\)$", padding)
    assert room and int(room[1]) >= 24, padding
    # The hub keeps it: Profile takes none of it back.
    views = [
        d for _, sels, d in rules("_profile.css") if any(s.startswith(".you-view") for s in sels)
    ]
    assert views
    assert not [d for d in views if any(k.startswith("padding") for k in d)]
