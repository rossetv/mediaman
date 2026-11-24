"""Showing and hiding uses the `hidden` attribute, never an inline display rule."""

from __future__ import annotations

import re
from pathlib import Path

import mediaman.web

_WEB = Path(mediaman.web.__file__).parent
_TEMPLATES = _WEB / "templates"
_JS = _WEB / "static" / "js"

_INLINE_NONE = re.compile(r"style\s*=\s*\"[^\"]*display\s*:\s*none", re.IGNORECASE)
# Measures a node with its box removed for a moment; it shows nothing.
_MEASURING = {"ui-feedback.js"}


def _is_watch(path: Path) -> bool:
    return path.name == "watch.html" or path.name.startswith("_watch_")


def test_no_template_hides_anything_with_an_inline_display_none() -> None:
    offenders = [
        str(p.relative_to(_TEMPLATES))
        for p in _TEMPLATES.rglob("*.html")
        if not _is_watch(p) and _INLINE_NONE.search(p.read_text())
    ]
    assert offenders == []


def test_no_script_shows_or_hides_with_style_display() -> None:
    offenders = [
        str(p.relative_to(_JS))
        for p in _JS.rglob("*.js")
        if "watch" not in p.relative_to(_JS).parts[:1]
        and p.name not in _MEASURING
        and re.search(r"\.style\.display\b", p.read_text())
    ]
    assert offenders == []


def test_the_abandon_dialog_and_its_season_list_start_hidden() -> None:
    html = (_TEMPLATES / "_dl_abandon_modal.html").read_text()
    assert re.search(r'id="abandon-modal"[^>]*\shidden\b', html)
    assert re.search(r'id="abandon-season-list"[^>]*\shidden\b', html)
