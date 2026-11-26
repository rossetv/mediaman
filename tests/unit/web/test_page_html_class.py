"""The classes a page puts on <html>, which the style sheets key their page-wide rules to."""

from __future__ import annotations

from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

TEMPLATES = Path(__file__).resolve().parents[3] / "src/mediaman/web/templates"


@pytest.fixture(scope="module")
def env() -> Environment:
    return Environment(loader=FileSystemLoader(TEMPLATES), autoescape=True)


@pytest.mark.parametrize(
    ("page", "classes"),
    [
        ("watch.html", "has-watch-search"),
        ("watch_player.html", "has-player"),
        ("account.html", "has-setg-pg"),
        ("settings.html", "has-setg-pg has-setg-rail"),
    ],
)
def test_a_page_marks_the_page_wide_classes_its_styles_need(env, page, classes) -> None:
    template = env.get_template(page)
    block = "".join(template.blocks["html_class"](template.new_context({})))
    assert block.strip() == f'class="{classes}"'


def test_the_base_page_puts_a_pages_classes_on_the_html_element(env) -> None:
    child = env.from_string(
        '{% extends "base.html" %}{% block html_class %} class="has-x"{% endblock %}'
    )
    html = child.render(nav_active="")
    assert '<html lang="en-GB" data-page="" class="has-x">' in html
