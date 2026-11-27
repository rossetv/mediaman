"""The AI sparkle twinkles while it thinks and never turns."""

from __future__ import annotations

import re
from pathlib import Path

CSS = Path(__file__).resolve().parents[3] / "src/mediaman/web/static/css"
SHEET = re.sub(r"/\*.*?\*/", "", (CSS / "_watch_making.css").read_text(encoding="utf-8"), flags=re.S)


def _keyframes(name: str) -> str:
    match = re.search(r"@keyframes " + name + r"\s*\{((?:[^{}]*\{[^{}]*\})*)\s*\}", SHEET)
    assert match, name
    return match.group(1)


def _names_on_sparkle() -> set[str]:
    """Every keyframe name a rule on the AI label's icon (or a custom property it reads) names."""
    names: set[str] = set()
    for head, body in re.findall(r"([^{}]+)\{([^{}]*)\}", SHEET):
        if "ai-label" in head or "--ai-twinkle" in body:
            names.update(re.findall(r"(?:animation(?:-name)?|--ai-twinkle[\w-]*):\s*([a-z][\w-]*)", body))
    return names - {"none", "var"}


def test_no_animation_of_the_sparkle_rotates() -> None:
    names = _names_on_sparkle()
    assert {"ai-twinkle-big", "ai-twinkle-small"} <= names
    for name in names:
        assert "rotate" not in _keyframes(name), name


def test_the_twinkle_moves_only_transform_and_opacity() -> None:
    for name in ("ai-twinkle-big", "ai-twinkle-small", "ai-twinkle-glow", "ai-twinkle-pulse"):
        props = set(re.findall(r"([a-z-]+)\s*:", _keyframes(name)))
        assert props <= {"transform", "opacity"}, (name, props)


def test_reduced_motion_pulses_opacity_slowly_and_keeps_the_stars_still() -> None:
    reduced = re.search(r"@media \(prefers-reduced-motion: reduce\)\s*\{(.*?)\n\}", SHEET, re.S)
    assert reduced and "ai-twinkle-pulse" in reduced.group(1)
