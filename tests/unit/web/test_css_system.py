"""The shared stylesheets keep one motion language, a 44px touch floor on phones, and modals
that open and close with motion.

These read the stylesheet rules, since no layout engine runs here. The Watch page's own
stylesheets (`_watch*.css`) are held to their own tests, except for the red tokens, which every
stylesheet, script and template shares.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS_DIR = Path(__file__).resolve().parents[3] / "src" / "mediaman" / "web" / "static" / "css"
SHARED = sorted(p for p in CSS_DIR.glob("_*.css") if not p.name.startswith("_watch"))
PHONE = "(max-width: 699.98px)"
REDUCE = "(prefers-reduced-motion: reduce)"


def _text(path: Path) -> str:
    return re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)


def _media_blocks(css: str, query: str) -> list[str]:
    """The bodies of every `@media` block whose condition list includes `query`."""
    out = []
    for m in re.finditer(r"@media([^{]*)\{", css):
        if query not in m.group(1):
            continue
        depth, i = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        out.append(css[m.end() : i - 1])
    return out


def _rules(css: str) -> list[tuple[list[str], str]]:
    return [
        ([s.strip() for s in head.split(",")], body)
        for head, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
    ]


def _declarations(prop: str) -> list[tuple[str, str]]:
    found = []
    for path in SHARED:
        for value in re.findall(rf"(?<![\w-]){prop}\s*:\s*([^;}}]+)", _text(path)):
            found.append((path.name, value.strip()))
    return found


LITERAL_TIME = re.compile(r"(?<![\w.-])\.?\d+(\.\d+)?m?s\b")


def test_transitions_use_the_motion_tokens() -> None:
    for name, value in _declarations("transition"):
        if value == "none":
            continue
        assert not LITERAL_TIME.search(re.sub(r"(?<![\d.])0s\b", "", value)), (
            f"{name}: literal duration in transition: {value}"
        )
        assert not re.search(r"\ball\b", value), f"{name}: transition on all: {value}"
        for part in value.split(","):
            # a `visibility 0s <delay>` part only flips at the end, so it has no curve
            if "var(--dur" in part and " 0s" not in part:
                assert "var(--ease-out)" in part, f"{name}: transition without ease-out: {part}"


def test_one_shot_animations_use_the_motion_tokens() -> None:
    for name, value in _declarations("animation"):
        if value.startswith("none") or "infinite" in value or "!important" in value:
            continue
        assert not LITERAL_TIME.search(value), f"{name}: literal duration in animation: {value}"


def test_every_control_is_44px_on_phones_and_tablets_with_the_bottom_nav() -> None:
    phone_rules = [
        rule
        for path in SHARED
        for block in _media_blocks(_text(path), PHONE)
        for rule in _rules(block)
    ]
    controls = {
        ".btn": "min-height",
        ".btn--icon": "min-width",
        ".link-danger": "min-height",
        ".btn-test": "min-height",
        ".snooze-btn": "min-height",
        ".unsub-btn-danger": "min-height",
        ".keep-dur-btn": "min-height",
    }
    for control, prop in controls.items():
        hit = [
            body
            for selectors, body in phone_rules
            if any(re.search(rf"{re.escape(control)}(?![\w-])", s) for s in selectors)
            and re.search(rf"{prop}:\s*44px", body)
        ]
        assert hit, f"{control} has no {prop}: 44px below 700px"


def test_toggles_reach_44px_on_phones_without_growing() -> None:
    phone_rules = [
        rule
        for path in SHARED
        for block in _media_blocks(_text(path), PHONE)
        for rule in _rules(block)
    ]
    assert any(
        any(s.endswith(".tog::before") for s in selectors) and "inset" in body
        for selectors, body in phone_rules
    )


def test_the_way_back_to_watch_reaches_44px_at_every_width() -> None:
    css = _text(CSS_DIR / "_nav.css")
    pill = int(re.search(r"\.nav-back,\s*\.nav-noscript \{[^}]*?min-height: (\d+)px", css).group(1))
    reach = int(re.search(r"\.nav-noscript::after \{[^}]*?inset: -(\d+)px 0", css).group(1))
    assert pill + 2 * reach >= 44
    phone = [rule for block in _media_blocks(css, PHONE) for rule in _rules(block)]
    heights = [body for heads, body in phone if ".nav-back" in heads and "height:" in body]
    assert heights and all(
        int(re.search(r"[^-]height: (\d+)px", b).group(1)) >= 44 for b in heights
    )


def test_what_is_scrolled_into_view_on_a_phone_stops_above_the_bottom_bar() -> None:
    css = _text(CSS_DIR / "_nav.css")
    phone = [rule for block in _media_blocks(css, PHONE) for rule in _rules(block)]
    (held,) = [body for heads, body in phone if "html:has(.nav-foot)" in heads and "scroll" in body]
    assert (
        int(re.search(r"scroll-padding-bottom: calc\(var\(--foot\) \+ (\d+)px", held).group(1)) > 0
    )
    assert re.search(r"--foot: calc\(\d+px \+ env\(safe-area-inset-bottom", held)
    # the strip is as tall as everything that keeps clear of it takes it to be
    assert any(".nav-foot" in heads and "height: var(--foot)" in body for heads, body in phone)


def test_no_offset_repeats_the_height_of_the_bar_at_the_foot() -> None:
    for name in ("_ui_feedback.css", "_settings_pg.css", "_watch_search.css"):
        css = _text(CSS_DIR / name)
        assert "var(--foot)" in css, name
        assert not re.search(r"calc\(\d+px \+ env\(safe-area-inset-bottom", css), name


def test_what_sticks_at_the_top_stands_where_the_bar_ends_and_at_the_top_where_it_is_gone() -> None:
    for name, part in (("_history.css", ".hx-tools"), ("_settings_pg.css", ".setg-pg .setg-rail")):
        tops = [
            top
            for block in re.findall(re.escape(part) + r" \{([^}]*)\}", _text(CSS_DIR / name))
            for top in re.findall(r"(?<![\w-])top: ([^;]+);", block)
        ]
        assert tops, name
        assert all("var(--nav-h)" in top or top == "auto" for top in tops), (name, tops)


def test_the_pairing_page_leaves_room_for_the_bar_at_the_foot_and_under_itself() -> None:
    (page,) = [
        body
        for heads, body in _rules(_text(CSS_DIR / "_pair.css"))
        if ".pair-pg" in heads and "48px" in body
    ]
    assert re.search(r"padding: 48px 20px calc\(\d+px \+ var\(--foot\)\)", page)


def test_the_bar_at_the_foot_leaves_what_stands_on_it_44px_on_a_short_phone_too() -> None:
    css = _text(CSS_DIR / "_nav.css")
    sizes = re.findall(r"--foot: calc\((\d+)px \+[^;]*;\s*--foot-gap: (\d+)px", css)
    assert len(sizes) == 2
    assert all(int(foot) - 2 * int(gap) >= 44 for foot, gap in sizes)


def test_nothing_in_the_bar_holds_what_is_fixed_at_the_foot_of_a_phone() -> None:
    nav, search = _text(CSS_DIR / "_nav.css"), _text(CSS_DIR / "_watch_search.css")
    own = [body for heads, body in _rules(nav) if ".nav-glass" in heads]
    assert own and not any("backdrop-filter" in body or "transform" in body for body in own)
    phone = [rule for block in _media_blocks(search, PHONE) for rule in _rules(block)]
    assert any(".wbar" in heads and "container-type: normal" in body for heads, body in phone)


def test_no_control_drops_back_to_40px_on_phones() -> None:
    for path in SHARED:
        css = _text(path)
        for query in ("(max-width: 639.98px)", PHONE):
            for block in _media_blocks(css, query):
                assert not re.search(r"(min-)?height:\s*40px", block), (
                    f"{path.name}: a 40px control height below 700px"
                )


def test_modals_open_with_modal_in_and_close_on_is_closing() -> None:
    css = _text(CSS_DIR / "_modal.css")
    assert re.search(r"@keyframes modal-in\s*\{", css)
    opening = [
        body
        for selectors, body in _rules(css)
        if ".modal-backdrop" in selectors and "animation" in body
    ]
    assert opening and "modal-in var(--dur) var(--ease-out)" in opening[0]
    closing = [
        body
        for selectors, body in _rules(css)
        if ".modal-backdrop.is-closing" in selectors and "animation" in body
    ]
    assert closing and "var(--dur-fast)" in closing[0]


def test_modals_do_not_move_under_reduced_motion() -> None:
    css = _text(CSS_DIR / "_modal.css")
    reduce = " ".join(_media_blocks(css, REDUCE))
    sheet_rules = [
        body for selectors, body in _rules(reduce) if any(".modal-sheet" in s for s in selectors)
    ]
    assert sheet_rules and all("animation: none" in body for body in sheet_rules)


def test_hidden_attribute_always_hides() -> None:
    css = _text(CSS_DIR / "_base.css")
    assert re.search(r"(^|\})\s*\[hidden\]\s*\{\s*display:\s*none\s*!important;?\s*\}", css)


def _phone_rules() -> list[tuple[list[str], str]]:
    return [
        rule
        for path in SHARED
        for block in _media_blocks(_text(path), PHONE)
        for rule in _rules(block)
    ]


def _phone_sets(selector: str, prop: str, value: str) -> bool:
    return any(
        any(re.search(rf"{re.escape(selector)}(?![\w-])", s) for s in selectors)
        and re.search(rf"{prop}:\s*{value}", body)
        for selectors, body in _phone_rules()
    )


def test_no_control_is_sized_to_44px_only_up_to_640() -> None:
    # the bottom nav starts at 700, so a 44px rule that stops at 640 leaves 640-699 short
    for path in SHARED:
        for block in _media_blocks(_text(path), "(max-width: 639.98px)"):
            assert not re.search(r"(?<![\w-])(min-)?(height|width):\s*44px", block), (
                f"{path.name}: a 44px target that stops at 640px"
            )


def _smallest(prop: str, *selectors: str) -> float:
    """The smallest number any shared rule for one of `selectors` sets `prop` to, in any media."""
    return min(float(n) for n, _ in _values(prop, *selectors))


def _values(prop: str, *selectors: str) -> list[tuple[str, str]]:
    """Every number, with its unit (empty when it has none), a shared rule for one of
    `selectors` sets `prop` to."""
    return [
        (n, unit)
        for path in SHARED
        for heads, body in _rules(_text(path))
        if any(re.search(rf"(?<![\w-]){re.escape(s)}$", head) for s in selectors for head in heads)
        for n, unit in re.findall(rf"(?<![\w-]){prop}:\s*([\d.]+)(px)?", body)
    ]


def _smallest_line(font: float, *selectors: str) -> float:
    """The smallest line, in px, a shared rule for one of `selectors` sets: a line height with
    no unit is a multiple of the font size, one in px is that many."""
    return min(
        float(n) if unit else float(n) * font for n, unit in _values("line-height", *selectors)
    )


def test_a_line_height_in_px_is_not_taken_for_a_multiple_of_the_font_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sheet = tmp_path / "_a.css"
    sheet.write_text(".a { line-height: 20px; } .b { line-height: 1.5; }", encoding="utf-8")
    monkeypatch.setattr(f"{__name__}.SHARED", [sheet])
    assert _smallest_line(12, ".a") == 20
    assert _smallest_line(12, ".b") == 18


def test_download_links_and_settings_fields_are_44px_below_700px() -> None:
    # A line of text, reached 15px above and below.
    link = (".dl-search-hint", ".dl-arr-link")
    line = _smallest_line(_smallest("font-size", *link), *link, "body")
    assert _phone_sets(".dl-arr-link::after", "inset", "-15px -6px")
    assert line + 2 * 15 >= 44
    assert _phone_sets(".setg-pg .inp", "min-height", "44px")


def test_keep_dialog_season_rows_are_44px_tap_targets_with_a_larger_checkbox() -> None:
    assert _phone_sets(".keep-season-item label", "min-height", "44px")
    css = _text(CSS_DIR / "_modal.css")
    box = [
        body
        for selectors, body in _rules(css)
        if any(".keep-season-item" in s and "checkbox" in s for s in selectors)
    ]
    assert box and re.search(r"width:\s*20px", box[0]) and re.search(r"height:\s*20px", box[0])


def _token(name: str) -> str:
    return re.search(rf"--{name}:\s*([^;]+);", _text(CSS_DIR / "_tokens.css")).group(1).strip()


def _luminance(rgb: tuple[float, ...]) -> float:
    lin = [
        c / 255 / 12.92 if c / 255 <= 0.03928 else ((c / 255 + 0.055) / 1.055) ** 2.4 for c in rgb
    ]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _hex(value: str) -> tuple[int, ...]:
    return tuple(int(value[i : i + 2], 16) for i in (1, 3, 5))


def test_danger_text_keeps_aa_contrast_on_every_red_tint_over_the_dark_surfaces() -> None:
    fg = _hex(_token("danger-text"))
    for tint in (
        "rgba-danger-bg",
        "rgba-danger-bg-hover",
        "rgba-danger-bg-soft",
        "rgba-danger-chip",
    ):
        *rgb, alpha = (float(x) for x in re.findall(r"[\d.]+", _token(tint)))
        for surface in ("s1", "s2", "s3"):
            under = _hex(_token(surface))
            bg = tuple(c * alpha + u * (1 - alpha) for c, u in zip(rgb, under, strict=True))
            hi, lo = sorted((_luminance(fg), _luminance(bg)), reverse=True)
            assert (hi + 0.05) / (lo + 0.05) >= 4.5, f"--danger-text on {tint} over --{surface}"


def test_a_leaving_confirm_dialog_keeps_catching_clicks() -> None:
    # pointer-events: none here let a second click land on whatever sits behind the dialog
    css = _text(CSS_DIR / "_ui_feedback.css")
    for selectors, body in _rules(css):
        if any(".ui-confirm-overlay" in s and "is-leaving" in s for s in selectors):
            assert "pointer-events" not in body


WEB_DIR = CSS_DIR.parents[1]
ALL_CSS = sorted(CSS_DIR.glob("_*.css"))
# every file that could carry a colour: stylesheets, scripts and templates (not the token file)
COLOUR_FILES = [
    p
    for p in [
        *ALL_CSS,
        *(WEB_DIR / "static" / "js").rglob("*.js"),
        *(WEB_DIR / "static" / "js").rglob("*.html"),
        *(WEB_DIR / "templates").rglob("*.html"),
    ]
    if p.name != "_tokens.css"
]


def test_the_red_tints_are_built_from_the_danger_token() -> None:
    red = _hex(_token("danger"))
    for tint in (
        "rgba-danger-bg",
        "rgba-danger-bg-hover",
        "rgba-danger-bg-soft",
        "rgba-danger-chip",
    ):
        rgb = tuple(int(x) for x in re.findall(r"\d+", _token(tint))[:3])
        assert rgb == red, f"--{tint} is not the --danger red"


def test_no_danger_red_is_written_out_beside_the_tokens() -> None:
    literal = re.compile(r"#ff453a|#ff7a72|rgba?\(\s*255\s*,\s*69\s*,\s*58", re.I)
    offenders = [p.name for p in COLOUR_FILES if literal.search(p.read_text(encoding="utf-8"))]
    assert not offenders, f"literal danger red outside the tokens: {offenders}"


@pytest.mark.parametrize("path", COLOUR_FILES, ids=lambda p: p.name)
def test_red_text_uses_the_text_token_not_the_fill_token(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".css":
        text = _text(path)
    # `color:` in CSS or inline, `style.color =` in a script; a fill (background, border) is not text
    assert not re.search(r"(?<![\w-])color\s*[:=]\s*['\"]?var\(--danger\)", text)
