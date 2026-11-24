"""The emailed download page's hero: a wide backdrop, or the poster as a poster, never stretched."""

from __future__ import annotations

import re
from pathlib import Path

import jinja2

WEB = Path(__file__).resolve().parents[3] / "src/mediaman/web"
_ENV = jinja2.Environment(loader=jinja2.FileSystemLoader(WEB / "templates"), autoescape=True)

POSTER = "https://image.tmdb.org/t/p/w300/p.jpg"
BACKDROP = "https://image.tmdb.org/t/p/w1280/b.jpg"


def _hero_images(**item: object) -> dict[str, str]:
    """Each hero picture's tag by its class."""
    item = {"title": "Ember Coast", "media_type": "movie", "notify_by_email": False, **item}
    html = _ENV.get_template("download.html").render(state="confirm", item=item)
    return {
        m.group(1): m.group(0) for m in re.finditer(r'<img class="dl-cinema__(\w+)"[^>]*>', html)
    }


def test_a_poster_is_never_the_hero_backdrop() -> None:
    images = _hero_images(poster_url=POSTER)

    assert set(images) == {"fill", "poster"}
    assert all("/w500/p.jpg" in tag and 'loading="lazy"' in tag for tag in images.values())


def test_the_backdrop_leads_and_the_poster_stands_in_when_it_is_unfit() -> None:
    images = _hero_images(poster_url=POSTER, backdrop_url=BACKDROP)

    assert list(images) == ["art", "fill", "poster"]
    assert BACKDROP in images["art"] and "data-wide-art" in images["art"]
    css = (WEB / "static/css/_download_page.css").read_text(encoding="utf-8")
    for stand_in in (".dl-cinema__fill", ".dl-cinema-info .dl-cinema__poster"):
        assert f".dl-cinema__art:not([data-broken]) ~ {stand_in}" in css


def test_no_hero_picture_tells_its_host_the_link_it_is_on() -> None:
    images = _hero_images(poster_url=POSTER, backdrop_url=BACKDROP)

    assert len(images) == 3
    assert all('referrerpolicy="no-referrer"' in tag for tag in images.values())


def test_the_rate_limited_page_retry_is_a_fingers_width_and_fits_a_phone_held_sideways() -> None:
    css = (WEB / "static/css/_download_page.css").read_text(encoding="utf-8")
    coarse = css[css.index("@media (pointer: coarse)") :]

    assert re.search(r"\.dl-expired-back a \{[^}]*min-height: 44px", coarse)
    sideways = re.search(r"@media \(max-height: 500px\) and[^{]*\{(.*?)\n\}", css, re.S)
    assert sideways and re.search(r"\.dl-expired \{ margin: 12px auto; padding: 14px", sideways[1])


def test_a_hero_with_no_picture_is_a_short_head() -> None:
    css = (WEB / "static/css/_download_page.css").read_text(encoding="utf-8")
    rule = re.search(r"\.dl-cinema:not\(:has\(img:not\(\[data-broken\]\)\)\) \{([^}]*)\}", css)

    assert rule and "min-height: 0" in rule[1] and "var(--hero-hi)" in rule[1]


def test_the_hero_takes_no_more_height_than_it_needs_on_a_short_screen() -> None:
    css = (WEB / "static/css/_download_page.css").read_text(encoding="utf-8")
    short = re.search(r"@media \(max-height: 500px\) \{(.*?)\n\}", css, re.S)

    assert short and re.search(r"\.dl-cinema \{[^}]*min-height: 0", short[1])
    assert re.search(r"\.dl-cinema__poster \{ width: min\([^}]*34vh\)", short[1])
