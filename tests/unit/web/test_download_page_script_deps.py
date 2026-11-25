"""download.html must load downloads/build_dom.js before download.js.

download.js draws the episodes with ``MM.downloads.buildDom.syncEpisodes``, which
build_dom.js alone defines, and ``defer`` scripts execute in document order.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
TEMPLATES = REPO / "src/mediaman/web/templates"

_BUILD_DOM_TAG = '<script src="/static/js/downloads/build_dom.js?v={{ static_v }}" defer></script>'
_DOWNLOAD_JS_TAG = '<script src="/static/js/download.js?v={{ static_v }}" defer></script>'


def _tpl(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def test_download_html_loads_build_dom_before_download_js() -> None:
    download = _tpl("download.html")
    assert _BUILD_DOM_TAG in download, (
        "download.html must load downloads/build_dom.js — download.js calls "
        "MM.downloads.buildDom.syncEpisodes(box, item)"
    )
    assert _DOWNLOAD_JS_TAG in download

    assert download.index(_BUILD_DOM_TAG) < download.index(_DOWNLOAD_JS_TAG), (
        "downloads/build_dom.js must be loaded before download.js — defer "
        "scripts execute in document order"
    )
