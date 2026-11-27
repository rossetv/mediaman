"""The storage meter and the profile button answer a pointer as the search field does: a lit pill
grows round them, on the field's own timing, without moving anything beside them, and never
sticks after a tap on a touch screen."""

from __future__ import annotations

import re
from pathlib import Path

CSS = Path(__file__).resolve().parents[3] / "src" / "mediaman" / "web" / "static" / "css"
BUTTONS = (".profile-btn", ".space-meter")
# What may change under a pointer: nothing that takes room, so nothing beside it moves.
ROOMLESS = {
    "opacity",
    "transform",
    "color",
    "filter",
    "background",
    "background-color",
    "outline-color",
}


def _rules(name: str) -> list[tuple[str, list[str], dict[str, str]]]:
    """(the at-rules round it, its selectors, its declarations) for every rule of a sheet."""
    css = re.sub(r"/\*.*?\*/", "", (CSS / name).read_text(encoding="utf-8"), flags=re.S)
    out, stack, start = [], [], 0
    for m in re.finditer(r"[{}]", css):
        head = css[start : m.start()].strip()
        if m.group() == "{":
            if head.startswith("@"):
                stack.append(head)
                start = m.end()
                continue
            end = css.index("}", m.end())
            body = css[m.end() : end]
            decls = {
                k.strip(): v.strip()
                for k, v in (d.split(":", 1) for d in body.split(";") if ":" in d)
            }
            out.append((" ".join(stack), [s.strip() for s in head.split(",")], decls))
            start = end + 1
        elif m.start() >= start:
            if stack:
                stack.pop()
            start = m.end()
    return out


def _all() -> list[tuple[str, list[str], dict[str, str]]]:
    return _rules("_nav.css") + _rules("_watch_space.css")


def _decls(selector: str, media: str = "", sheet: str = "") -> dict[str, str]:
    found: dict[str, str] = {}
    for at, sels, decls in _rules(sheet) if sheet else _all():
        if selector in sels and at == media:
            found.update(decls)
    return found


def test_a_pointer_left_on_either_after_a_tap_lights_nothing() -> None:
    for at, sels, _ in _all():
        if "prefers-reduced-motion: reduce" in at:
            continue  # what reduced motion takes back lights nothing
        for sel in sels:
            if any(sel.startswith(b) for b in BUTTONS) and ":hover" in sel:
                assert "(hover: hover)" in at, f"{sel} would stay lit after a tap"


def test_the_keyboard_an_open_panel_and_a_press_get_the_same_lit_pill() -> None:
    for button in BUTTONS:
        for state, media in (
            (":focus-visible", "@media (hover: hover)"),
            ('[aria-expanded="true"]', ""),
            (":active", ""),
        ):
            lit = _decls(f"{button}{state}::before", media)
            assert lit.get("opacity") == "1" and lit.get("transform") == "none", button + state


def test_what_lights_under_a_pointer_takes_no_room() -> None:
    hovered = [
        (sel, decls)
        for at, sels, decls in _all()
        for sel in sels
        if any(sel.startswith(b) for b in BUTTONS) and ":hover" in sel
    ]
    assert len(hovered) >= 4
    for sel, decls in hovered:
        assert set(decls) <= ROOMLESS, f"{sel} sets {set(decls) - ROOMLESS}"


def test_reduced_motion_keeps_the_light_and_drops_the_growth() -> None:
    reduce = {
        s
        for at, sels, decls in _all()
        if "prefers-reduced-motion: reduce" in at and decls.get("transform") == "none"
        for s in sels
    }
    assert {".profile-btn::before", ".space-meter::before"} <= reduce
    # What grows inside them under a pointer or the keyboard stays still there.
    for _at, sels, decls in _all():
        for sel in sels:
            inside = any(sel.startswith(b) for b in BUTTONS) and " " in sel
            if inside and (":hover" in sel or ":focus-visible" in sel) and "transform" in decls:
                assert sel in reduce, f"{sel} grows under reduced motion"
