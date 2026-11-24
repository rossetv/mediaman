"""Render-smoke coverage for the weekly newsletter template.

Catches Jinja2 syntax breakage and asserts that the expected
sections reach the rendered output against realistic context data.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader


@pytest.fixture
def env() -> Environment:
    template_dir = (
        Path(__file__).resolve().parents[4]
        / "src"
        / "mediaman"
        / "services"
        / "mail"
        / "newsletter"
        / "templates"
    )
    return Environment(loader=FileSystemLoader(str(template_dir)), autoescape=True)


def _full_context(dry_run: bool = False) -> dict:
    return {
        "report_date": "19 April 2026",
        "storage": {
            "total_bytes": 22 * 1024**4,
            "used_bytes": 18 * 1024**4,
            "free_bytes": 4 * 1024**4,
            "by_type": {"movie": 8 * 1024**4, "show": 7 * 1024**4, "anime": 3 * 1024**4},
        },
        "reclaimed_week": 420 * 1024**3,
        "reclaimed_month": int(1.2 * 1024**4),
        "reclaimed_total": int(8.7 * 1024**4),
        "scheduled_items": [
            {
                "title": "Ad Astra",
                "type_label": "Movie",
                "poster_url": "https://example/p/1.jpg",
                "file_size_bytes": 42 * 1024**3,
                "added_days_ago": 180,
                "days_left": 14,
                "last_watched_info": None,
                "keep_url": "https://example/keep/tok1",
                "is_reentry": False,
            },
            {
                "title": "Westworld S4",
                "type_label": "TV · Season 4",
                "poster_url": "https://example/p/2.jpg",
                "file_size_bytes": 68 * 1024**3,
                "added_days_ago": 300,
                "days_left": 9,
                "last_watched_info": None,
                "keep_url": "https://example/keep/tok2",
                "is_reentry": True,
            },
        ],
        "deleted_items": [
            {
                "title": "Forgotten Film",
                "poster_url": "https://example/p/3.jpg",
                "deleted_date": "3 days ago",
                "file_size_bytes": 12 * 1024**3,
                "media_type": "movie",
                "redownload_url": "https://example/download/rd1",
            },
        ],
        "this_week_items": [
            {
                "id": 1,
                "title": "Dune: Part Two",
                "media_type": "movie",
                "category": "trending",
                "description": "",
                "reason": "Massive box office",
                "poster_url": "https://example/p/4.jpg",
                "tmdb_id": 693134,
                "rating": 8.5,
                "rt_rating": 95,
                "download_url": "https://example/download/d1",
            },
            {
                "id": 2,
                "title": "Oppenheimer",
                "media_type": "movie",
                "category": "trending",
                "description": "",
                "reason": "Best Picture winner",
                "poster_url": "https://example/p/5.jpg",
                "tmdb_id": 872585,
                "rating": 8.3,
                "rt_rating": 93,
                "download_state": "in_library",
            },
            {
                "id": 3,
                "title": "Severance S2",
                "media_type": "tv",
                "category": "personal",
                "description": "",
                "reason": "You loved S1",
                "poster_url": "https://example/p/6.jpg",
                "tmdb_id": 95396,
                "rating": 8.8,
                "rt_rating": None,
                "download_state": "downloading",
            },
        ],
        "dashboard_url": "https://example",
        "dry_run": dry_run,
        "base_url": "https://example",
        "unsubscribe_url": "https://example/unsubscribe?token=xyz",
    }


def test_full_render(env: Environment) -> None:
    html = env.get_template("newsletter.html").render(**_full_context())

    assert html.startswith("<!DOCTYPE html>")
    assert "</html>" in html
    assert "{{" not in html
    assert "{%" not in html

    assert "scheduled for deletion" in html
    assert "14 days left" in html  # the first card's own deadline
    assert "9 days left" in html  # the second card, and the header (the soonest)
    assert "Ad Astra" in html
    assert "Westworld S4" in html
    assert 'href="https://example/keep/tok1"' in html
    assert 'href="https://example/keep/tok2"' in html
    assert "Previously kept" in html  # re-entry warning
    assert "Dune: Part Two" in html
    assert "Severance S2" in html
    assert "In Library" in html
    assert "Downloading" in html
    assert "Trending This Week" in html
    assert "Based on Your Watch History" in html
    assert "Deleted Since Last Report" in html

    # Removed section should not appear
    assert "Last Week" not in html


def test_a_deleted_season_is_named_on_its_card_and_a_movie_has_none(env: Environment) -> None:
    ctx = _full_context()
    ctx["scheduled_items"] = []
    film = ctx["deleted_items"][0]
    template = env.get_template("newsletter.html")
    assert "Season" not in template.render(**ctx)

    season = {**film, "title": "Fargo", "media_type": "tv", "season_number": 2}
    ctx["deleted_items"] = [{**film, "season_number": None}, season]

    assert re.findall(r"Season \d+|Season None", template.render(**ctx)) == ["Season 2"]


def test_a_deleted_season_zero_is_called_specials(env: Environment) -> None:
    ctx = _full_context()
    ctx["deleted_items"] = [{**ctx["deleted_items"][0], "media_type": "tv", "season_number": 0}]

    html = env.get_template("newsletter.html").render(**ctx)

    assert ">Specials</div>" in html
    assert "Season 0" not in html


def test_empty_scheduled_and_deleted(env: Environment) -> None:
    ctx = _full_context()
    ctx["scheduled_items"] = []
    ctx["deleted_items"] = []

    html = env.get_template("newsletter.html").render(**ctx)

    assert "scheduled for deletion" not in html
    assert "Deleted Since Last Report" not in html
    assert "Trending This Week" in html
    assert "Open mediaman" in html


def test_dry_run_banner_renders(env: Environment) -> None:
    html = env.get_template("newsletter.html").render(**_full_context(dry_run=True))

    assert "DRY RUN MODE" in html


def test_singular_day_left(env: Environment) -> None:
    ctx = _full_context()
    for item in ctx["scheduled_items"]:
        item["days_left"] = 1

    html = env.get_template("newsletter.html").render(**ctx)

    assert "1 day left" in html
    assert "1 days left" not in html


def test_header_counts_down_to_the_soonest_deadline(env: Environment) -> None:
    html = env.get_template("newsletter.html").render(**_full_context())

    countdown = re.search(r'class="hero-countdown"[^>]*>(.*?)</td>', html, re.S).group(1)
    assert "9 days left" in countdown


def test_every_poster_image_declares_its_size(env: Environment) -> None:
    """Mail clients that drop the stylesheet still need a width and height."""
    html = env.get_template("newsletter.html").render(**_full_context())
    imgs = re.findall(r"<img\b[^>]*>", html)
    posters = [tag for tag in imgs if 'class="poster"' in tag]
    assert posters
    for tag in posters:
        assert re.search(r'\swidth="\d+"', tag), tag
        assert re.search(r'\sheight="\d+"', tag), tag


def test_fluid_posters_scale_with_their_cell_without_distortion(env: Environment) -> None:
    """The height attribute would otherwise beat the width and crop posters wide."""
    html = env.get_template("newsletter.html").render(**_full_context())
    fluid = [tag for tag in re.findall(r"<img\b[^>]*>", html) if 'width="100"' in tag]
    assert fluid
    for tag in fluid:
        assert "width:100%;height:auto" in tag, tag


def test_legend_lists_other_when_the_grey_segment_is_drawn(env: Environment) -> None:
    ctx = _full_context()
    ctx["storage"]["used_bytes"] = 20 * 1024**4  # 2 TB beyond the library types

    html = env.get_template("newsletter.html").render(**ctx)

    chips = re.search(r'<div [^>]*class="chips">(.*?)</div>', html, re.S).group(1)
    assert "Other 2.0 TB" in chips
    assert "#48484a" in chips


def test_legend_omits_other_when_nothing_is_left_over(env: Environment) -> None:
    html = env.get_template("newsletter.html").render(**_full_context())

    chips = re.search(r'<div [^>]*class="chips">(.*?)</div>', html, re.S).group(1)
    assert "Other" not in chips


def test_never_watched_cannot_wrap(env: Environment) -> None:
    html = env.get_template("newsletter.html").render(**_full_context())

    assert re.search(r"white-space:nowrap[^>]*>Never(&nbsp;| )watched<", html)
    assert "Never watched" not in html


def _tags(html: str, name: str) -> list[str]:
    return re.findall(rf"<{name}\b[^>]*>", html)


def test_text_and_background_colours_are_inline_not_only_in_the_stylesheet(
    env: Environment,
) -> None:
    """Clients that drop <style> must still get light text on the dark background."""
    html = env.get_template("newsletter.html").render(**_full_context())

    assert re.search(r"<body[^>]*background:#000000[^>]*color:#ffffff", html)
    frame = re.search(r'<table[^>]*class="mm-email"[^>]*>', html).group(0)
    assert "background:#000000" in frame
    assert "color:#ffffff" in frame
    assert "font-family:" in frame
    title = re.search(r"<div[^>]*>Ad Astra</div>", html).group(0)
    assert "color:#ffffff" in title


def test_buttons_carry_their_own_background_and_padding(env: Environment) -> None:
    html = env.get_template("newsletter.html").render(**_full_context())

    for href in ("https://example/keep/tok1", "https://example/download/d1"):
        cell = re.search(r'<td[^>]*>(?=<a href="' + re.escape(href) + '")', html).group(0)
        assert "background:#0071e3" in cell
        assert "border-radius:6px" in cell
        link = re.search(r'<a href="' + re.escape(href) + r'"[^>]*>', html).group(0)
        assert "padding:" in link
        assert "color:#ffffff" in link


def test_poster_grids_are_tables_not_css_grids(env: Environment) -> None:
    html = env.get_template("newsletter.html").render(**_full_context())

    assert "display:grid" not in html
    assert "display:flex" not in html
    assert html.count('class="grid"') >= 3  # scheduled, deleted, recommended


def test_every_poster_has_the_title_as_alt_text(env: Environment) -> None:
    html = env.get_template("newsletter.html").render(**_full_context())

    alts = {re.search(r'alt="([^"]*)"', tag).group(1) for tag in _tags(html, "img")}
    assert alts == {
        "Ad Astra",
        "Westworld S4",
        "Forgotten Film",
        "Dune: Part Two",
        "Oppenheimer",
        "Severance S2",
    }


def test_declares_a_dark_colour_scheme(env: Environment) -> None:
    html = env.get_template("newsletter.html").render(**_full_context())

    assert '<meta name="color-scheme" content="dark">' in html
    assert '<meta name="supported-color-schemes" content="dark">' in html


def test_footer_text_and_unsubscribe_link_are_readable(env: Environment) -> None:
    html = env.get_template("newsletter.html").render(**_full_context())

    footer = html[html.index("You&#39;re receiving this") - 200 :]
    assert "rgba(" not in footer
    assert "color:#a1a1a6" in footer
    link = re.search(r'<a href="https://example/unsubscribe\?token=xyz"[^>]*>', footer).group(0)
    assert "color:#d1d1d6" in link
    assert "underline" in link


def _luminance(hex_colour: str) -> float:
    def channel(i: int) -> float:
        c = int(hex_colour[i : i + 2], 16) / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5)


def test_button_text_reads_at_the_aa_contrast(env: Environment) -> None:
    html = env.get_template("newsletter.html").render(**_full_context())

    link = re.search(r'<a href="https://example/keep/tok1"[^>]*>', html).group(0)
    cell = re.search(
        r'<td[^>]*background:(#[0-9a-f]{6})[^>]*>(?=<a href="https://example/keep)', html
    )
    text = re.search(r"color:(#[0-9a-f]{6})", link).group(1)
    light, dark = sorted([_luminance(text), _luminance(cell.group(1))], reverse=True)
    assert (light + 0.05) / (dark + 0.05) >= 4.5


def test_every_button_is_a_thumb_tall(env: Environment) -> None:
    ctx = _full_context()
    ctx["unsubscribe_url"] = ""
    html = env.get_template("newsletter.html").render(**ctx)

    links = re.findall(r'<a href="[^"]*" class="btn[^"]*" style="([^"]*)"', html)
    assert len(links) >= 4  # keep, download, re-download, open
    for style in links:
        pad = int(re.search(r"padding:(\d+)px", style).group(1))
        line = int(re.search(r"line-height:(\d+)px", style).group(1))
        assert 2 * pad + line >= 44, style


def test_the_unsubscribe_link_is_a_thumb_tall(env: Environment) -> None:
    ctx = _full_context()
    ctx["unsubscribe_url"] = "https://mm.example/unsubscribe/abc"
    html = env.get_template("newsletter.html").render(**ctx)

    style = re.search(r'<a href="https://mm.example/unsubscribe/abc" style="([^"]*)"', html)[1]
    pad = int(re.search(r"padding:(\d+)px", style).group(1))
    line = int(re.search(r"line-height:(\d+)px", style).group(1))
    assert 2 * pad + line >= 44, style


def test_a_long_unbroken_title_may_break_instead_of_widening_the_message(
    env: Environment,
) -> None:
    ctx = _full_context()
    ctx["scheduled_items"][0]["title"] = "Extended_Cut_2160p_Remux" * 4

    html = env.get_template("newsletter.html").render(**ctx)

    title = re.search(r"<div[^>]*>Extended_Cut[^<]*</div>", html).group(0)
    assert "overflow-wrap:anywhere" in title


@pytest.mark.parametrize(
    ("size", "shown"),
    [
        (500_000, "488 KB"),
        (900, "900 B"),
        (1024**2 - 1, "1 MB"),
        (1024**3 - 1, "1.0 GB"),
        (int(1023.96 * 1024**3), "1.0 TB"),
        (int(1023.9 * 1024**3), "1023.9 GB"),
    ],
)
def test_sizes_hand_over_to_the_next_unit_before_they_read_1024(
    env: Environment, size: int, shown: str
) -> None:
    ctx = _full_context()
    ctx["scheduled_items"][0]["file_size_bytes"] = size

    html = env.get_template("newsletter.html").render(**ctx)

    card = html[html.index('class="item-title">Ad Astra') : html.index("keep/tok1")]
    assert f">{shown}</div>" in card


@pytest.mark.parametrize(
    ("days", "shown"),
    [(335, "Added 335 days ago"), (1, "Added 1 day ago"), (0, "Added today")],
)
def test_a_scheduled_card_says_how_long_ago_it_was_added(
    env: Environment, days: int, shown: str
) -> None:
    ctx = _full_context()
    ctx["scheduled_items"][0]["added_days_ago"] = days

    html = env.get_template("newsletter.html").render(**ctx)

    assert shown in html
    assert f"Added {days}d" not in html
