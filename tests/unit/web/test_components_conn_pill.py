"""The connection pill carries its own announce region, separate from its label."""

from __future__ import annotations

from pathlib import Path

import jinja2

TEMPLATES = Path(__file__).resolve().parents[3] / "src/mediaman/web/templates"


def test_conn_pill_has_empty_status_region_apart_from_label():
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES), autoescape=True)
    html = str(env.get_template("_components.html").module.conn_pill("plex"))
    # The label must not be the live region, or the page-load auto-check is announced.
    assert 'role="status" data-conn-announce></span>' in html
    assert 'data-conn-label>Untested</span>' in html
    assert html.count('role="status"') == 1
