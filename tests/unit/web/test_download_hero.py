"""The emailed download page's hero: a wide backdrop, or the poster as a poster, never stretched."""

from __future__ import annotations

import re
from pathlib import Path

import jinja2

WEB = Path(__file__).resolve().parents[3] / "src/mediaman/web"
_ENV = jinja2.Environment(loader=jinja2.FileSystemLoader(WEB / "templates"), autoescape=True)

POSTER = "https://image.tmdb.org/t/p/w300/p.jpg"
BACKDROP = "https://image.tmdb.org/t/p/w1280/b.jpg"


def _css(name: str) -> str:
    return (WEB / "static/css" / name).read_text(encoding="utf-8")


def _page(**item: object) -> str:
    item = {"title": "Ember Coast", "media_type": "movie", "notify_by_email": False, **item}
    return _ENV.get_template("download.html").render(state="confirm", item=item)


def _hero_images(**item: object) -> dict[str, str]:
    """Each hero picture's tag by its class."""
    tags = re.finditer(
        r'<img class="(?:watch-banner__bg )?watch-banner__([\w-]+)"[^>]*>', _page(**item)
    )
    return {m.group(1): m.group(0) for m in tags}


def test_a_poster_is_never_the_hero_backdrop() -> None:
    images = _hero_images(poster_url=POSTER)

    assert set(images) == {"bg--fill", "poster"}
    assert all("/w500/p.jpg" in tag and 'loading="lazy"' in tag for tag in images.values())


def test_the_backdrop_leads_and_the_poster_stands_in_when_it_is_unfit() -> None:
    images = _hero_images(poster_url=POSTER, backdrop_url=BACKDROP)

    assert list(images) == ["bg--art", "bg--fill", "poster"]
    assert BACKDROP in images["bg--art"] and "data-wide-art" in images["bg--art"]
    assert all("data-broken-on-error" in tag for tag in images.values())
    for stand_in in (".watch-banner__bg--fill", ".watch-banner__poster"):
        assert f".watch-banner__bg--art:not([data-broken]) ~ {stand_in}" in _css("_watch.css")


def test_no_hero_picture_tells_its_host_the_link_it_is_on() -> None:
    images = _hero_images(poster_url=POSTER, backdrop_url=BACKDROP)

    assert len(images) == 3
    assert all('referrerpolicy="no-referrer"' in tag for tag in images.values())


def test_a_page_with_only_something_to_say_has_a_fingers_button_and_fits_a_phone_held_sideways() -> (
    None
):
    css = _css("_share.css")

    assert re.search(r"\.share-note \.btn \{[^}]*min-height: 46px", css)
    sideways = re.search(
        r"@media \(max-height: 500px\) and \(orientation: landscape\) \{(.*?)\n\}", css, re.S
    )
    assert sideways and re.search(r"\.share-note \.login-card \{ padding: 18px", sideways[1])


def test_a_hero_with_no_picture_keeps_no_room_for_one_on_a_phone() -> None:
    phone = re.search(r"@media \(max-width: 699\.98px\) \{(.*?)\n\}", _css("_share.css"), re.S)

    assert phone and re.search(
        r"\.share-hero:not\(:has\(> img:not\(\[data-broken\]\)\)\) \{ --top: 104px", phone[1]
    )


def test_the_backdrop_waits_to_be_known_for_a_wide_picture_before_it_shows() -> None:
    css = _css("_watch.css")

    assert re.search(
        r"@media \(scripting: enabled\) \{\s*"
        r"\.watch-banner__bg--art:not\(\[data-fit\]\) \{ opacity: 0; \}",
        css,
    )
    assert '.watch-banner__bg--art[data-fit="at-once"]' in css


def test_the_reason_has_a_heading_and_is_no_taller_than_its_own_text() -> None:
    html = _page(reason="Because sand.")

    assert re.search(r'<div class="ttl__reason"><h2[^>]*>.*?Why this one</h2>', html)
    assert re.search(
        r"\.share-body \.ttl__reason \{ align-self: start; \}", _css("_download_page.css")
    )


def test_the_button_comes_before_the_summary_and_the_cast() -> None:
    html = _page(description="Salt.", cast_list=[{"name": "Ada Vale", "character": "Lead"}])

    assert html.index('id="btn-download"') < html.index("Salt.") < html.index("Ada Vale")


def test_a_failure_stands_after_its_button_and_is_read_out() -> None:
    html = _page()

    for part in ("try-again", "refused"):
        answer = re.search(rf'<template data-part="{part}">(.*?)</template>', html, re.S)[1]
        button = re.search(r'<(?:button|a) [^>]*aria-describedby="dl-failed"', answer)
        message = answer.index('id="dl-failed" role="alert"')
        assert button and button.start() < message
    assert re.search(r'<span class="sr-only" role="status" data-said>', html)


def test_a_long_word_wraps_inside_the_page_body() -> None:
    rule = re.search(r"\.share-body \{([^}]*)\}", _css("_download_page.css"))

    assert rule and "overflow-wrap: anywhere" in rule[1]


def _queued_card(**context: object) -> str:
    item = {"id": "radarr:Ember Coast", "title": "Ember Coast", "media_type": "movie"}
    item.update(state="searching", state_label="Searching", progress=0, **context.pop("item", {}))
    return _ENV.get_template("_dl_hero_card.html").render(item=item, **context)


def test_the_queued_card_is_filled_with_the_wide_backdrop_when_there_is_one() -> None:
    wide = _queued_card(item={"poster_url": POSTER}, hero_backdrop=BACKDROP)
    poster_only = _queued_card(item={"poster_url": POSTER})

    assert f'class="dl-hero-bg" data-bg-url="{BACKDROP}"' in wide
    assert f'<img src="{POSTER}"' in wide
    assert f'class="dl-hero-bg" data-bg-url="{POSTER}"' in poster_only


def test_a_title_pill_never_wraps_its_count() -> None:
    css = _css("_dl.css")
    rule = re.search(r"\.dl-hero-episodes-toggle,\s*\.dl-compact-episodes-toggle \{([^}]*)\}", css)

    assert rule and "white-space: nowrap" in rule[1]
