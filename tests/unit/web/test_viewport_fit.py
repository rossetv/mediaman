"""Pages with a viewport tag extend under the notch, or every safe-area inset reads zero."""

from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[3] / "src" / "mediaman" / "web" / "templates"


@pytest.mark.parametrize("name", ["base.html", "tv.html"])
def test_viewport_covers_the_screen(name):
    html = (TEMPLATES / name).read_text()
    tag = next(line for line in html.splitlines() if 'name="viewport"' in line)
    assert "viewport-fit=cover" in tag
