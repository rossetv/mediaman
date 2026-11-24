"""The list's sortable headers are real controls that carry their sort state."""

from __future__ import annotations

import re
from pathlib import Path

from tests.helpers.factories import insert_media_item

_CSS_DIR = Path(__file__).parents[5] / "src" / "mediaman" / "web" / "static" / "css"


def _list(client, conn, **params) -> str:
    insert_media_item(conn, id="m1", title="Dune", plex_rating_key="rk-m1", file_path="/f")
    return client.get("/watch/list", params=params).text


def test_list_table_sort_uses_real_controls(client, conn):
    """<th onclick=...> is not keyboard-activatable; each sortable header holds a
    real control with the column it sorts, and aria-sort says the current sort."""
    html = _list(client, conn)
    assert "onclick" not in html
    assert html.count('class="th-sort"') == 4
    for col in ("name", "size", "watched", "deletion"):
        assert f'data-sort-col="{col}"' in html
    assert "aria-sort=" in html


def test_the_sorted_header_says_its_direction_and_its_control_flips_it(client, conn):
    html = _list(client, conn, sort="deletion_asc")

    (th,) = re.findall(
        r'<th class="sortable col-del" aria-sort="(\w+)">\s*<a class="th-sort" href="([^"]+)"', html
    )
    assert th[0] == "ascending"
    assert "sort=deletion_desc" in th[1]
    assert html.count('aria-sort="none"') == 3


def test_list_table_sort_button_styled():
    """The .th-sort control must visually match the cell."""
    css = "\n".join(p.read_text() for p in sorted(_CSS_DIR.glob("_*.css")))
    assert ".tbl thead th.sortable .th-sort" in css
