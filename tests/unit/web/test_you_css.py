"""The pane over Watch's stylesheet (_you.css) and where Watch puts the pane (watch.html)."""

from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[3] / "src/mediaman/web"
CSS = re.sub(r"/\*.*?\*/", "", (WEB / "static/css/_you.css").read_text(), flags=re.S)
WATCH = (WEB / "templates/watch.html").read_text()


def _block(at: str) -> str:
    """The body of the at-rule that starts with *at*."""
    start = CSS.index(at + " {") + len(at) + 2
    depth, i = 1, start
    while depth:
        depth += {"{": 1, "}": -1}.get(CSS[i], 0)
        i += 1
    return CSS[start : i - 1]


def _rules(css: str) -> dict[str, str]:
    return {sel.strip(): body for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)}


def test_with_reduced_motion_the_pane_and_its_views_only_fade_on_the_fast_duration() -> None:
    calm = _rules(_block("@media (prefers-reduced-motion: reduce)"))
    moving = [sel for sel in calm if "transition" in calm[sel]]
    assert {".you-view", ".you-view.is-on"} <= set(moving)
    assert any(".you__panel" in sel and ".you__veil" in sel for sel in moving)
    for sel in moving:
        for part in re.findall(r"transition:([^;]*);", calm[sel])[0].split(","):
            prop, duration = part.split()[:2]
            assert prop in {"opacity", "visibility"}, (sel, part)
            assert duration in {"var(--dur-fast)", "0s"}, (sel, part)
    # what slides in full motion stands still
    assert "transform: none" in calm[".you-view"]
    assert all("transform: none" in calm[sel] for sel in moving if ".you__panel" in sel)


def test_every_motion_of_the_pane_runs_on_the_app_s_tokens() -> None:
    durations = re.findall(r"transition:([^;]*);", CSS)
    for value in durations:
        for part in value.split(","):
            assert re.search(r"var\(--dur(-fast|-slow)?\)|\b0s\b|^\s*none\s*$", part), part


def test_on_a_phone_the_pane_is_a_sheet_from_the_foot_with_a_handle_the_finger_owns() -> None:
    phone = _rules(_block("@media (max-width: 699.98px)"))
    sheet = phone[".you__panel, .you.is-wide .you__panel"]
    assert "inset: auto 0 0" in sheet and "translateY(100%)" in sheet
    assert "touch-action: none" in phone[".you__grab"]
    assert "touch-action: none" in phone[".you__bar"]
    assert "transition: none" in phone[".you__panel.is-dragged"]


def test_the_pane_stands_over_the_page_and_under_a_question_or_a_toast() -> None:
    assert "z-index: var(--z-modal)" in _rules(CSS)[".you"]


def test_watch_loads_the_pane_s_script_before_the_one_that_starts_the_address() -> None:
    assert WATCH.index("watch/address.js") < WATCH.index("watch/you.js") < WATCH.index("/watch.js")
    assert "_you.css" in WATCH
    assert '{% include "_you.html" %}' in WATCH.split("{% block overlays %}")[1]
