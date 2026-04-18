"""Tests for the download-ready email path.

The outer ``check_download_notifications`` function coordinates DB,
Mailgun, Radarr, and Sonarr — too much infrastructure for a unit test.
These tests focus on the security-critical part: the Jinja template
must escape every TMDB-sourced field so a malicious free-text value
(e.g. a crafted director string) cannot inject HTML/JS into the email.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader


def _load_template():
    template_dir = (
        Path(__file__).parent.parent.parent.parent
        / "src"
        / "mediaman"
        / "web"
        / "templates"
    )
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=True,
    )
    return env.get_template("email/download_ready.html")


def _render(**overrides):
    template = _load_template()
    defaults = {
        "title": "Example",
        "poster_src": "",
        "meta": {
            "year": "2026",
            "media_label": "Movie",
            "runtime": "120",
            "director": "Jane Doe",
        },
        "ratings": {
            "rating": "",
            "imdb_rating": "",
            "rt_rating": "",
        },
        "description": "",
    }
    defaults.update(overrides)
    return template.render(**defaults)


class TestDownloadReadyTemplate:
    def test_renders_with_basic_context(self):
        html = _render()
        assert "Example" in html
        assert "2026" in html
        assert "Directed by Jane Doe" in html
        assert "READY TO WATCH" in html

    def test_title_escapes_html(self):
        """A crafted title (e.g. Plex metadata tampering) must be escaped."""
        html = _render(title='<script>alert(1)</script>')
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_director_escapes_html(self):
        """Director is a TMDB free-text field and MUST NOT inject HTML."""
        meta = {
            "year": "2026",
            "media_label": "Movie",
            "runtime": "90",
            "director": '"><img src=x onerror=alert(1)>',
        }
        html = _render(meta=meta)
        # The raw `<img ...>` tag must never appear — every `<` has to be escaped.
        assert "<img src=x" not in html
        assert "<img " not in html
        # The escaped form must be present — `<` becomes `&lt;`.
        assert "&lt;img" in html
        # The stray `>` at the end must also be escaped.
        assert "&gt;" in html

    def test_description_escapes_html(self):
        html = _render(description='</div><script>alert("xss")</script>')
        assert "<script>" not in html
        assert "&lt;/div&gt;" in html

    def test_ratings_escape_html(self):
        """Rating fields come from OMDb/TMDB — still untrusted."""
        ratings = {
            "rating": '8.2',
            "imdb_rating": '7.5"><script>alert(1)</script>',
            "rt_rating": '92%',
        }
        html = _render(ratings=ratings)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
        # The benign ratings still render visibly.
        assert "8.2" in html
        assert "92%" in html

    def test_meta_line_uses_middle_dot_separator(self):
        """The meta line preserves the original &middot; separator between parts."""
        html = _render()
        # &middot; is a template literal so Jinja's autoescape leaves it
        # alone; the email client sees the middle-dot character.
        assert "&middot;" in html
        # Parts are present around the separator.
        assert "Movie" in html
        assert "120 min" in html

    def test_no_safe_filter_on_new_variables(self):
        """Regression guard: the template must not reintroduce ``|safe`` on
        user-sourced data. If someone adds it back, this test fails."""
        template_path = (
            Path(__file__).parent.parent.parent.parent
            / "src" / "mediaman" / "web" / "templates"
            / "email" / "download_ready.html"
        )
        src = template_path.read_text()
        assert "|safe" not in src and "| safe" not in src, (
            "download_ready.html must not use |safe — every field is "
            "untrusted TMDB/OMDb data."
        )

    def test_ratings_section_hidden_when_all_empty(self):
        html = _render()
        # No rating row at all when nothing is present.
        assert "IMDb" not in html
        assert "&#9733;" not in html

    def test_poster_rendered_when_present(self):
        html = _render(poster_src="https://image.tmdb.org/t/p/w500/abc.jpg")
        assert "https://image.tmdb.org/t/p/w500/abc.jpg" in html
