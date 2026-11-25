"""The shared look of buttons, pills and the sign-in pages: colours that keep their contrast,
and sizes that fit a finger and a narrow phone."""

from __future__ import annotations

import re
from pathlib import Path

CSS_DIR = Path(__file__).resolve().parents[3] / "src" / "mediaman" / "web" / "static" / "css"
WHITE = (255.0, 255.0, 255.0)


def _css(name: str) -> str:
    return re.sub(r"/\*.*?\*/", "", (CSS_DIR / name).read_text(encoding="utf-8"), flags=re.S)


def _body(name: str, selector: str) -> str:
    """The declarations of every rule of *name* headed by exactly *selector*."""
    rules = re.findall(r"([^{}]+)\{([^{}]*)\}", _css(name))
    return "".join(body for head, body in rules if selector in [s.strip() for s in head.split(",")])


def _token(name: str) -> str:
    return re.search(rf"--{name}:\s*([^;]+);", _css("_tokens.css")).group(1).strip()


def _rgb(value: str) -> tuple[float, ...]:
    if value.startswith("#"):
        return tuple(float(int(value[i : i + 2], 16)) for i in (1, 3, 5))
    return tuple(float(x) for x in re.findall(r"[\d.]+", value))


def _contrast(fg: tuple[float, ...], bg: tuple[float, ...]) -> float:
    def luminance(rgb: tuple[float, ...]) -> float:
        lin = [
            c / 255 / 12.92 if c / 255 <= 0.03928 else ((c / 255 + 0.055) / 1.055) ** 2.4
            for c in rgb
        ]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    hi, lo = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _on(tint: str, surface: str) -> tuple[float, ...]:
    """The colour of the tint token laid over the surface token."""
    *rgb, alpha = _rgb(_token(tint))
    return tuple(
        c * alpha + u * (1 - alpha) for c, u in zip(rgb, _rgb(_token(surface)), strict=True)
    )


def _var(body: str, prop: str) -> str:
    return re.search(rf"(?<![\w-]){prop}:\s*var\(--([\w-]+)\)", body).group(1)


def test_white_text_keeps_aa_on_a_primary_button_under_the_pointer() -> None:
    for sheet, button, under in (
        ("_buttons.css", ".btn--primary", ":hover"),
        ("_login.css", ".login-btn", ":hover"),
        ("_download_page.css", ".btn-download-hero", ':not([aria-disabled="true"]):hover'),
    ):
        for state in ("", under):
            fill = _rgb(_token(_var(_body(sheet, button + state), "background")))
            assert _contrast(WHITE, fill) >= 4.5, button + state


def test_blue_and_purple_text_keep_aa_on_their_tints_over_every_surface_a_pill_sits_on() -> None:
    wearers = {
        ".pill--tv": "accent-text",
        ".pill--anime": "purple-text",
        ".btn--accent-soft": "accent-text",
        ".keep-trigger": "accent-text",
        ".btn-sm-caret": "accent-text",
    }
    for selector, text in wearers.items():
        body = _body("_buttons.css", selector)
        assert _var(body, "color") == text, selector
        for surface in ("s1", "s2", "s3", "s4"):
            ratio = _contrast(_rgb(_token(text)), _on(_var(body, "background"), surface))
            assert ratio >= 4.5, f"{selector} over --{surface}"


def test_a_button_is_reached_from_44px_on_tablets_and_touch_screens_without_growing() -> None:
    css = _css("_buttons.css")
    block = css.split("@media (min-width: 700px) and (max-width: 1023.98px), (pointer: coarse) {")[
        1
    ]
    reach = re.search(r"\.btn::after \{([^}]*)\}", block.split("\n}")[0]).group(1)
    # half of what the button lacks to 44px, above and below, whatever its size
    assert "inset: min(0px, calc((100% - 44px) / 2)) 0" in reach
    assert "min-height" not in block.split("\n}")[0]


def test_a_sign_in_button_s_label_has_air_when_it_takes_two_lines() -> None:
    body = _body("_login.css", ".login-btn")
    top = re.search(r"(?<![\w-])padding:\s*(\d+)px", body)
    assert top and int(top.group(1)) >= 8
    assert "text-align: center" in body


def test_the_profile_page_ends_with_room_under_its_last_card() -> None:
    wide = _css("_profile.css").split("@media (min-width: 700px) {")[1].split("\n}")[0]
    room = re.search(r"\.profile-pg \.container \{ padding-bottom: (\d+)px; \}", wide)
    assert room and int(room.group(1)) >= 48
