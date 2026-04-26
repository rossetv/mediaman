"""Render-smoke coverage for the weekly newsletter template.

Catches Jinja2 syntax breakage and asserts that the expected
sections reach the rendered output against realistic context data.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader


@pytest.fixture
def env() -> Environment:
    template_dir = Path(__file__).resolve().parents[3] / "src" / "mediaman" / "web" / "templates"
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
        "grace_days": 14,
        "unsubscribe_url": "https://example/unsubscribe?token=xyz",
    }


def test_full_render(env: Environment) -> None:
    html = env.get_template("email/newsletter.html").render(**_full_context())

    assert html.startswith("<!DOCTYPE html>")
    assert "</html>" in html
    assert "{{" not in html and "{%" not in html

    assert "scheduled for deletion" in html
    assert "14 days left" in html
    assert "Ad Astra" in html
    assert "Westworld S4" in html
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


def test_empty_scheduled_and_deleted(env: Environment) -> None:
    ctx = _full_context()
    ctx["scheduled_items"] = []
    ctx["deleted_items"] = []

    html = env.get_template("email/newsletter.html").render(**ctx)

    assert "scheduled for deletion" not in html
    assert "Deleted Since Last Report" not in html
    assert "Trending This Week" in html
    assert "Open Mediaman Dashboard" in html


def test_dry_run_banner_renders(env: Environment) -> None:
    html = env.get_template("email/newsletter.html").render(**_full_context(dry_run=True))

    assert "DRY RUN MODE" in html


def test_singular_grace_day(env: Environment) -> None:
    ctx = _full_context()
    ctx["grace_days"] = 1

    html = env.get_template("email/newsletter.html").render(**ctx)

    assert "1 day left" in html
    assert "1 days left" not in html
