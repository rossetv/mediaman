"""The shared stylesheets keep one motion language, a 44px touch floor on phones, and modals
that open and close with motion.

These read the stylesheet rules, since no layout engine runs here. The Watch page's own
stylesheets (`_watch*.css`) are held to their own tests.
"""

from __future__ import annotations

import re
from pathlib import Path

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
        ".keep-trigger": "min-height",
        ".page-btn": "min-width",
        ".link-danger": "min-height",
        ".btn-test": "min-height",
        ".snooze-btn": "min-height",
        ".unsub-btn-danger": "min-height",
        ".keep-dur-btn": "min-height",
        ".snooze-option": "min-height",
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
