"""Layout of the download-ready email on a phone."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

_TEMPLATES = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "mediaman"
    / "services"
    / "downloads"
    / "templates"
)


def _render(**overrides: object) -> str:
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES)), autoescape=True)
    ctx = {
        "title": "Example",
        "season_label": "",
        "poster_src": "",
        "backdrop_src": "",
        "meta": {"year": "2026", "media_label": "Movie", "runtime": "", "director": ""},
        "ratings": {"rating": "", "imdb_rating": "", "rt_rating": ""},
        "description": "",
    }
    ctx.update(overrides)
    return env.get_template("download_ready.html").render(**ctx)


def test_a_long_unbroken_title_and_description_may_break_instead_of_widening_the_message() -> None:
    html = _render(title="Documentary_Extended_Cut_2160p_Remux", description="x" * 120)

    for text in ("Documentary_Extended_Cut_2160p_Remux", "x" * 120):
        tag = html[: html.rindex(text)].rsplit("<div", 1)[1]
        assert "overflow-wrap:anywhere" in tag


def test_a_phone_gets_narrower_side_padding() -> None:
    html = _render()

    assert "@media (max-width: 520px)" in html
    assert 'class="pad"' in html
