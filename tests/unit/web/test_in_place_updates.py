"""Pages update in place: the list never reloads, and what comes and goes moves nothing else."""

from __future__ import annotations

import re
from pathlib import Path

_WEB = Path(__file__).resolve().parents[3] / "src/mediaman/web"
_JS = _WEB / "static/js"


def test_watch_list_scripts_never_reload():
    for name in ("list.js", "admin.js"):
        src = (_JS / "watch" / name).read_text(encoding="utf-8")
        assert "location.reload" not in src, name


_CSS = _WEB / "static/css"


def _blocks(name: str, selector: str) -> list[str]:
    """Every declaration block whose selector list contains `selector`."""
    css = re.sub(r"/\*.*?\*/", "", (_CSS / name).read_text(encoding="utf-8"), flags=re.S)
    return [
        m.group(2)
        for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css)
        if selector in [part.strip() for part in m.group(1).split(",")]
    ]


class TestScanStatusAndToastPlacement:
    def test_scan_status_takes_the_room_that_is_left_so_it_cannot_move_the_button(self):
        status = " ".join(_blocks("_watch_space.css", ".space__scan-status"))
        assert "flex: 1" in status and "min-width: 0" in status
        assert "min-width" in " ".join(_blocks("_watch_space.css", ".space__scan"))

    def test_desktop_toasts_sit_at_the_bottom_clear_of_page_header_controls(self):
        css = (_CSS / "_ui_feedback.css").read_text(encoding="utf-8")
        desktop = css[css.index("@media (min-width: 700px)") :]
        desktop = desktop[: desktop.index("/* Mobile")]
        assert "top: auto" in desktop
        assert "bottom: var(--toast-bottom)" in desktop

    def test_tile_lift_only_where_a_pointer_can_hover(self):
        css = (_CSS / "_tiles.css").read_text(encoding="utf-8")
        assert re.search(r"@media \(hover: hover\) \{\s*\.tile:hover \{ transform", css)
        assert not re.search(r"^\.tile:hover \{", css, flags=re.M)


class TestAbandonDialogCss:
    def test_abandon_dialog_stacks_like_the_confirm_dialog_on_phones(self):
        css = re.sub(r"/\*.*?\*/", "", (_CSS / "_dl.css").read_text(encoding="utf-8"), flags=re.S)
        phone = css[css.index("@media (max-width: 699.98px)") :]
        assert re.search(r"\.abandon-modal-actions \{ flex-direction: column; \}", phone)
        assert re.search(
            r"\.abandon-modal-actions \.btn \{[^}]*width: 100%[^}]*min-height: 44px", phone
        )
        assert re.search(r"\.abandon-modal-body h2 \{ font-size: 18px; \}", phone)
