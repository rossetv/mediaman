"""Regression test: download.html must load downloads/build_dom.js.

``download.js``'s ``buildHeroCard()`` calls
``MM.downloads.buildDom.buildHero(item)``, but ``MM.downloads.buildDom`` is
only ever defined by ``static/js/downloads/build_dom.js`` — a module only
``downloads.html`` (the dashboard-ish listing page) loaded, not
``download.html`` (the per-token confirm page). Before ``download.js`` was
made parseable (curly-quote fix), the page's script never ran, so the
missing dependency was invisible; once it parses, a successful download
throws a ``TypeError`` in the ``.then`` handler, which the generic
``.catch`` reports to the user as "Network error — please try again" — on a
download that had actually succeeded.

``defer`` scripts execute in document order, so ``build_dom.js`` must also
appear *before* ``download.js`` in the template.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
TEMPLATES = REPO / "src/mediaman/web/templates"

_BUILD_DOM_TAG = '<script src="/static/js/downloads/build_dom.js" defer></script>'
_DOWNLOAD_JS_TAG = '<script src="/static/js/download.js" defer></script>'


def _tpl(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def test_download_html_loads_build_dom_before_download_js() -> None:
    download = _tpl("download.html")
    assert _BUILD_DOM_TAG in download, (
        "download.html must load downloads/build_dom.js — download.js's "
        "buildHeroCard() calls MM.downloads.buildDom.buildHero(item)"
    )
    assert _DOWNLOAD_JS_TAG in download

    assert download.index(_BUILD_DOM_TAG) < download.index(_DOWNLOAD_JS_TAG), (
        "downloads/build_dom.js must be loaded before download.js — defer "
        "scripts execute in document order"
    )
