"""The emailed download page loads downloads/build_dom.js before download.js.

download.js draws the episodes with ``MM.downloads.buildDom.syncEpisodes``, which
build_dom.js alone defines, and ``defer`` scripts execute in document order.
"""

from __future__ import annotations

import re
from pathlib import Path

import jinja2

TEMPLATES = Path(__file__).resolve().parents[3] / "src/mediaman/web/templates"


def test_the_download_page_runs_build_dom_before_download_js() -> None:
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES), autoescape=True)
    item = {"title": "Film", "media_type": "tv", "tmdb_id": 7, "action": "download"}
    html = env.get_template("download.html").render(state="confirm", item=item, token="t")
    scripts = re.findall(r'<script src="/static/js/([^"?]+)[^>]*\bdefer\b', html)
    assert "downloads/build_dom.js" in scripts and "download.js" in scripts, scripts
    assert scripts.index("downloads/build_dom.js") < scripts.index("download.js")
