"""Render-smoke coverage for the weekly newsletter template.

Catches Jinja2 syntax breakage and asserts that the expected
sections reach the rendered output against realistic context data.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

from mediaman.crypto import generate_download_token, generate_unsubscribe_token, sign_poster_url
from mediaman.crypto.tokens import generate_keep_token
from mediaman.services.mail.newsletter.render import render_to_fit
from mediaman.services.mail.plaintext import html_to_text


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

    assert "Leaving soon" in html
    assert "Leaving in 14 days" in html
    assert "Leaving in 9 days" in html
    assert "Ad Astra" in html
    assert "Westworld S4" in html
    assert 'href="https://example/keep/tok1"' in html
    assert 'href="https://example/keep/tok2"' in html
    assert html.count("It was kept before, and that time is up.") == 1  # the re-entry
    assert "Dune: Part Two" in html
    assert "Severance S2" in html
    assert "In the library" in html
    assert "Downloading" in html
    assert "Worth a look this week" in html
    assert "Picked for you" in html
    assert "Gone since the last mail" in html
    assert 'href="https://example/download/d1"' in html
    assert 'href="https://example/download/rd1"' in html
    assert 'href="https://example/unsubscribe?token=xyz"' in html

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

    assert ">Specials · deleted 3 days ago" in html
    assert "Season 0" not in html


@pytest.mark.parametrize("poster", ["", "https://example/p.jpg"], ids=["no_poster", "poster"])
def test_what_a_title_or_a_reason_holds_is_escaped(env: Environment, poster: str) -> None:
    ctx = _full_context()
    for key in ("scheduled_items", "deleted_items", "this_week_items"):
        for item in ctx[key]:
            item.update(title='"><b>x', reason="<b>y", type_label="<b>z", poster_url=poster)

    html = env.get_template("newsletter.html").render(**ctx)

    assert "<b>" not in html
    assert "&lt;b&gt;y" in html and "&lt;b&gt;z" in html
    assert html.count("&#34;&gt;&lt;b&gt;x") == 12  # each title, and its picture's name or place


def test_empty_scheduled_and_deleted(env: Environment) -> None:
    ctx = _full_context()
    ctx["scheduled_items"] = []
    ctx["deleted_items"] = []

    html = env.get_template("newsletter.html").render(**ctx)

    assert "Leaving soon" not in html
    assert "Gone since the last mail" not in html
    assert "Worth a look this week" in html
    assert "Open mediaman" in html


def test_dry_run_banner_renders(env: Environment) -> None:
    html = env.get_template("newsletter.html").render(**_full_context(dry_run=True))

    assert "A trial run: nothing will really be deleted." in html
    assert "trial run" not in env.get_template("newsletter.html").render(**_full_context())


def test_a_title_with_one_day_left_is_leaving_tomorrow(env: Environment) -> None:
    ctx = _full_context()
    for item in ctx["scheduled_items"]:
        item["days_left"] = 1

    html = env.get_template("newsletter.html").render(**ctx)

    assert html.count("Leaving tomorrow") == 2
    assert "1 day" not in html.replace("14 days", "")


@pytest.mark.parametrize(
    ("leaving", "line"),
    [
        (2, "2 titles are leaving. Keep what you want to stay."),
        (1, "1 title is leaving. Keep what you want to stay."),
        (0, "New picks for the week."),
    ],
)
def test_the_inbox_line_says_what_the_mail_is_for(
    env: Environment, leaving: int, line: str
) -> None:
    ctx = _full_context()
    ctx["scheduled_items"] = ctx["scheduled_items"][:leaving]

    text = html_to_text(env.get_template("newsletter.html").render(**ctx))

    assert text.startswith(line)


def test_what_can_be_done_comes_first_and_disk_space_last(env: Environment) -> None:
    text = html_to_text(env.get_template("newsletter.html").render(**_full_context()))

    order = [
        "Leaving soon",
        "Picked for you",
        "Worth a look this week",
        "Gone since the last mail",
        "4.0 TB free of 22.0 TB",
        "Open mediaman (https://example)",
        "Unsubscribe (https://example/unsubscribe?token=xyz)",
    ]
    assert sorted(order, key=text.index) == order


def test_the_plain_text_part_has_every_link_of_the_mail(env: Environment) -> None:
    html = env.get_template("newsletter.html").render(**_full_context())

    links = re.findall(r'href="([^"]+)"', html)
    assert len(links) == 6
    text = html_to_text(html)
    assert [link for link in links if f"({link})" not in text] == []


def test_every_poster_image_declares_its_size(env: Environment) -> None:
    """Mail clients that drop the stylesheet still need a width and height."""
    html = env.get_template("newsletter.html").render(**_full_context())
    imgs = re.findall(r"<img\b[^>]*>", html)
    assert len(imgs) == 6
    for tag in imgs:
        assert re.search(r'\swidth="\d+"', tag), tag
        assert re.search(r'\sheight="\d+"', tag), tag


def test_fluid_posters_scale_with_their_cell_without_distortion(env: Environment) -> None:
    """The height attribute would otherwise beat the width and crop posters wide."""
    html = env.get_template("newsletter.html").render(**_full_context())
    imgs = re.findall(r"<img\b[^>]*>", html)
    assert imgs
    for tag in imgs:
        assert re.search(r"width:100%;max-width:\d+px;height:auto", tag), tag


def test_a_title_without_a_poster_shows_its_name_in_the_posters_place(env: Environment) -> None:
    ctx = _full_context()
    ctx["scheduled_items"][0]["poster_url"] = ""

    html = env.get_template("newsletter.html").render(**ctx)

    place = re.search(r'<td class="pic" aria-hidden="true"[^>]*>([^<]*)</td>', html)
    assert place and place.group(1) == "Ad Astra"


def test_the_plain_text_part_names_a_title_without_a_poster_once(env: Environment) -> None:
    ctx = _full_context()
    for key in ("scheduled_items", "deleted_items", "this_week_items"):
        for item in ctx[key]:
            item["poster_url"] = ""

    text = html_to_text(env.get_template("newsletter.html").render(**ctx))

    for title in ("Ad Astra", "Forgotten Film", "Dune: Part Two"):
        assert text.count(title) == 1


@pytest.mark.parametrize(("used_tb", "share"), [(11, 50), (20, 91), (30, 100), (0, 0)])
def test_the_bar_is_filled_to_the_share_of_the_disk_in_use(
    env: Environment, used_tb: int, share: int
) -> None:
    ctx = _full_context()
    ctx["storage"]["used_bytes"] = used_tb * 1024**4

    html = env.get_template("newsletter.html").render(**ctx)

    track = re.search(r'class="track".*?</table>', html, re.S).group(0)
    assert re.findall(r'<td width="(\d+)%"[^>]*width:(\d+)%', track) == [(str(share),) * 2]


def test_only_the_types_that_take_space_are_named(env: Environment) -> None:
    ctx = _full_context()
    template = env.get_template("newsletter.html")
    assert "8.0 TB movies · 7.0 TB TV · 3.0 TB anime\n" in html_to_text(template.render(**ctx))

    ctx["storage"]["by_type"] = {"movie": 8 * 1024**4, "show": 0, "anime": 0}
    assert "8.0 TB movies\n" in html_to_text(template.render(**ctx))


def test_never_watched_cannot_wrap(env: Environment) -> None:
    html = env.get_template("newsletter.html").render(**_full_context())

    assert "Never&nbsp;watched" in html
    assert "Never watched" not in html


def _tags(html: str, name: str) -> list[str]:
    return re.findall(rf"<{name}\b[^>]*>", html)


def test_text_and_background_colours_are_inline_not_only_in_the_stylesheet(
    env: Environment,
) -> None:
    """Clients that drop <style> must still get dark text on the light background."""
    html = env.get_template("newsletter.html").render(**_full_context())
    html = re.sub(r"<style>.*?</style>", "", html, flags=re.S)

    assert re.search(r"<body[^>]*background:#f2f2f4", html)
    card = re.search(r'<td class="card"[^>]*>', html).group(0)
    assert 'bgcolor="#ffffff"' in card and "background:#ffffff" in card
    title = re.search(r"<div[^>]*>Ad Astra</div>", html).group(0)
    assert "color:#1d1d1f" in title
    assert "font-family:" in html[: html.index(title)].rsplit("<td", 1)[1]
    assert "rgba(" not in html


def test_buttons_carry_their_own_background_and_padding(env: Environment) -> None:
    html = env.get_template("newsletter.html").render(**_full_context())

    for href in ("https://example/keep/tok1", "https://example/download/d1"):
        cell = re.search(r'<td[^>]*>(?=<a href="' + re.escape(href) + '")', html).group(0)
        assert "background:#0071e3" in cell
        assert "border-radius:23px" in cell
        link = re.search(r'<a href="' + re.escape(href) + r'"[^>]*>', html).group(0)
        assert "padding:" in link
        assert "color:#ffffff" in link


def test_poster_grids_are_tables_not_css_grids(env: Environment) -> None:
    html = env.get_template("newsletter.html").render(**_full_context())

    assert "display:grid" not in html
    assert "display:flex" not in html
    assert html.count('<table role="presentation" class="item"') == 3  # scheduled, deleted
    assert html.count('<table role="presentation" class="picks"') == 2
    assert "<table" not in re.sub(r'<table role="presentation"', "", html)


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


def test_declares_both_colour_schemes(env: Environment) -> None:
    html = env.get_template("newsletter.html").render(**_full_context())

    assert '<meta name="color-scheme" content="light dark">' in html
    assert '<meta name="supported-color-schemes" content="light dark">' in html


def test_footer_text_and_unsubscribe_link_are_readable(env: Environment) -> None:
    html = env.get_template("newsletter.html").render(**_full_context())

    footer = html[html.index("You get this because") - 200 :]
    assert "rgba(" not in footer
    assert "color:#5f5f66" in footer
    link = re.search(
        r'<a class="link" href="https://example/unsubscribe\?token=xyz"[^>]*>', footer
    ).group(0)
    assert "color:#0060c7" in link
    assert "underline" in link


def _luminance(hex_colour: str) -> float:
    def channel(i: int) -> float:
        c = int(hex_colour[i : i + 2], 16) / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5)


def _contrast(one: str, other: str) -> float:
    light, dark = sorted([_luminance(one), _luminance(other)], reverse=True)
    return (light + 0.05) / (dark + 0.05)


def test_button_text_reads_at_the_aa_contrast(env: Environment) -> None:
    html = env.get_template("newsletter.html").render(**_full_context())

    link = re.search(r'<a href="https://example/keep/tok1"[^>]*>', html).group(0)
    cell = re.search(
        r'<td[^>]*background:(#[0-9a-f]{6})[^>]*>(?=<a href="https://example/keep)', html
    )
    text = re.search(r"color:(#[0-9a-f]{6})", link).group(1)
    assert _contrast(text, cell.group(1)) >= 4.5


_ON_A_PLATE = ("okb", "warn", "chip", "pic", "b-wait", "b-dl")


def test_all_text_reads_at_the_aa_contrast_in_the_reader_s_dark_mail(env: Environment) -> None:
    html = env.get_template("newsletter.html").render(**_full_context())
    dark = re.search(r"@media \(prefers-color-scheme:dark\)\{(.*?)\n\}", html, re.S).group(1)
    rules = dict(re.findall(r"\.([\w-]+)\{([^}]*)\}", dark))

    def colour(name: str, prop: str) -> str | None:
        found = re.search(rf"(?<![\w-]){prop}:(#[0-9a-f]{{6}}) !important", rules[name])
        return found.group(1) if found else None

    for name in rules:
        text = colour(name, "color")
        plates = ["card", "bg"] if name not in _ON_A_PLATE else [name]
        for plate in plates if text else []:
            assert _contrast(text, colour(plate, "background")) >= 4.5, (name, plate)
    assert all(colour(name, "color") for name in (*_ON_A_PLATE, "t1", "t2", "link", "bad"))


def test_all_text_reads_at_the_aa_contrast_without_the_styles(env: Environment) -> None:
    ctx = _full_context(dry_run=True)
    ctx["this_week_items"][0]["download_state"] = "queued"
    html = env.get_template("newsletter.html").render(**ctx)

    texts = set(re.findall(r"(?<![\w-])color:(#[0-9a-f]{6})", html.split("<body")[1]))
    plates = re.findall(
        r'class="([\w-]+)" align="center" bgcolor="(#[0-9a-f]{6})"[^>]*?[; ]color:(#[0-9a-f]{6})',
        html,
    )
    assert {name for name, _, _ in plates} >= {"warn", "okb", "b-dl", "b-wait"}
    for name, plate, text in plates:
        assert _contrast(text, plate) >= 4.5, name
    for text in texts - {text for _, _, text in plates} - {"#ffffff", "#f2f2f4"}:
        least = 3 if text == "#0071e3" else 4.5  # the name at the top, which is large
        assert min(_contrast(text, "#ffffff"), _contrast(text, "#f2f2f4")) >= least, text


def test_every_button_is_a_thumb_tall(env: Environment) -> None:
    ctx = _full_context()
    ctx["unsubscribe_url"] = ""
    html = env.get_template("newsletter.html").render(**ctx)

    links = re.findall(r'<a[^>]* style="(display:block[^"]*)"', html)
    assert len(links) == 5  # two of keep, download, download again, open
    for style in links:
        pad = int(re.search(r"padding:(\d+)px", style).group(1))
        line = int(re.search(r"line-height:(\d+)px", style).group(1))
        assert 2 * pad + line >= 44, style


def test_the_unsubscribe_link_is_a_thumb_tall(env: Environment) -> None:
    ctx = _full_context()
    ctx["unsubscribe_url"] = "https://mm.example/unsubscribe/abc"
    html = env.get_template("newsletter.html").render(**ctx)

    style = re.search(r'href="https://mm.example/unsubscribe/abc" style="([^"]*)"', html)[1]
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

    card = html[html.index(">Ad Astra</div>") : html.index("keep/tok1")]
    assert f"Movie · {shown}<br>" in card


@pytest.mark.parametrize(
    ("days", "shown"),
    [(335, "added 335 days ago"), (1, "added 1 day ago"), (0, "added today")],
)
def test_a_scheduled_card_says_how_long_ago_it_was_added(
    env: Environment, days: int, shown: str
) -> None:
    ctx = _full_context()
    ctx["scheduled_items"][0]["added_days_ago"] = days

    html = env.get_template("newsletter.html").render(**ctx)

    card = html[html.index(">Ad Astra</div>") : html.index("keep/tok1")]
    assert shown in card


def test_with_the_disk_unreadable_it_says_nothing_of_free_space(env: Environment) -> None:
    ctx = _full_context()
    ctx["storage"].update(used_bytes=18 * 1024**4, total_bytes=18 * 1024**4, free_bytes=0)

    html = env.get_template("newsletter.html").render(**ctx)

    assert " free" not in html_to_text(html).replace("Made free", "")
    assert 'class="track"' not in html
    assert "8.0 TB movies · 7.0 TB TV · 3.0 TB anime\nMade free: 420.0 GB" in html_to_text(html)


def test_a_full_disk_still_says_nothing_is_free(env: Environment) -> None:
    ctx = _full_context()
    ctx["storage"].update(used_bytes=22 * 1024**4, total_bytes=22 * 1024**4, free_bytes=0)

    html = env.get_template("newsletter.html").render(**ctx)

    assert "0 B free of 22.0 TB" in html_to_text(html)
    assert 'class="track"' in html


def _long_context(leaving: int) -> dict:
    """A mail of *leaving* titles with links as long as real ones, in no order of deadline."""
    key, site = "k" * 64, "https://mediaman.home.example"

    def link(action: str, title: str) -> str:
        token = generate_download_token(
            email="a.long.name@example.com",
            action=action,
            title=title,
            media_type="movie",
            tmdb_id=1234567,
            recommendation_id=12345,
            secret_key=key,
        )
        return f"{site}/download/{token}"

    def keep(number: int) -> str:
        token = generate_keep_token(
            media_item_id="123456", action_id=number, expires_at=1893456000, secret_key=key
        )
        return f"{site}/keep/{token}"

    ctx = _full_context()
    item, gone, pick = (ctx[k][0] for k in ("scheduled_items", "deleted_items", "this_week_items"))
    poster = site + sign_poster_url("123456", key)
    title = "A Title That Is Forty Characters Long Ok"
    ctx["scheduled_items"] = [
        item
        | {
            "title": title,
            "poster_url": poster,
            "keep_url": keep(10_000 + n),
            "days_left": n * 7 % leaving,
        }
        for n in range(leaving)
    ]
    ctx["deleted_items"] = [
        gone | {"title": title, "poster_url": poster, "redownload_url": link("redownload", title)}
    ] * 10
    ctx["this_week_items"] = [
        pick
        | {
            "title": title,
            "category": category,
            "reason": "r" * 160,
            "poster_url": f"https://image.tmdb.org/t/p/w300/{'p' * 27}.jpg",
            "download_url": link("download", title),
        }
        for category in ("trending", "personal")
    ] * 9
    unsubscribe = generate_unsubscribe_token(email="a.long.name@example.com", secret_key=key)
    ctx["dashboard_url"] = site
    ctx["unsubscribe_url"] = f"{site}/unsubscribe?token={unsubscribe}"
    return ctx


def _fitted(env: Environment, ctx: dict) -> str:
    return render_to_fit(env.get_template("newsletter.html"), **ctx)


@pytest.mark.parametrize("leaving", [24, 30, 60, 100, 300])
def test_a_long_mail_ends_before_gmail_cuts_it_even_with_every_link_tracked(
    env: Environment, leaving: int
) -> None:
    ctx = _long_context(leaving)

    html = _fitted(env, ctx)

    tracked = re.sub(r'href="', 'href="' + "t" * 300, html)
    assert len(tracked.encode()) <= 102_000
    assert ctx["unsubscribe_url"] in html
    assert html.endswith("</html>")


@pytest.mark.parametrize("leaving", [24, 30, 60, 100])
def test_every_leaving_title_of_a_long_mail_keeps_its_link(env: Environment, leaving: int) -> None:
    ctx = _long_context(leaving)

    html = _fitted(env, ctx)

    assert set(re.findall(r'href="([^"]*/keep/[^"]*)"', html)) == {
        item["keep_url"] for item in ctx["scheduled_items"]
    }
    assert "more in mediaman" not in html.split("Gone since")[0]
    assert html_to_text(html).startswith(f"{leaving} titles are leaving.")


def test_a_mail_too_long_for_every_title_lists_the_soonest_and_links_to_the_rest(
    env: Environment,
) -> None:
    ctx = _long_context(300)

    text = html_to_text(_fitted(env, ctx))

    soonest = sorted(ctx["scheduled_items"], key=lambda item: item["days_left"])
    listed = [item["keep_url"] for item in soonest if item["keep_url"] in text]
    assert listed == [item["keep_url"] for item in soonest[: len(listed)]]
    assert len(listed) > 80
    assert f"and {300 - len(listed)} more in mediaman ({ctx['dashboard_url']})" in text
    assert f"10 titles, in mediaman ({ctx['dashboard_url']})" in text
    assert text.startswith("300 titles are leaving.")


def test_a_long_mail_shows_the_soonest_with_pictures_and_the_rest_in_short_rows(
    env: Environment,
) -> None:
    ctx = _long_context(24)
    last = max(ctx["scheduled_items"], key=lambda item: item["days_left"])
    last |= {"title": "Fargo", "type_label": "TV · Season 2"}

    html = _fitted(env, ctx)

    rows = re.findall(r">(Leaving (?!soon)[^<]*)</(div|span)>", html.split("Picked for you")[0])
    assert [tag for _, tag in rows] == ["div"] * 12 + ["span"] * 12
    assert [text for text, _ in rows[:3]] == [
        "Leaving today",
        "Leaving tomorrow",
        "Leaving in 2 days",
    ]
    assert "Fargo · Season 2<br>" in html
    for style in re.findall(r'<a class="link" href="[^"]*/keep/[^"]*" style="([^"]*)"', html):
        assert "display:inline-block;padding:12px" in style
    assert html.count("Download again") == 10


def test_picks_and_gone_give_way_before_a_leaving_title_does(env: Environment) -> None:
    html = _fitted(env, _long_context(60))

    assert html.count("/keep/") == 60
    assert html.count(">Download<") < 18
    assert html.count(">Download again<") == 3
    assert "and 7 more in mediaman" in html


def test_a_mail_that_fits_is_left_as_it_is(env: Environment) -> None:
    ctx = _full_context()
    html = env.get_template("newsletter.html").render(**ctx)

    assert _fitted(env, ctx) == html


def test_a_row_without_a_button_keeps_its_gap_on_a_phone(env: Environment) -> None:
    ctx = _full_context()
    ctx["scheduled_items"][0]["keep_url"] = ""
    ctx["deleted_items"][0]["redownload_url"] = ""

    html = env.get_template("newsletter.html").render(**ctx)

    assert html.count('class="txt lone"') == 2 and html.count('class="txt"') == 1
    phone = html.split("@media (max-width:520px){")[1].split("</style>")[0]
    assert ".item .lead{width:72px !important;padding:0 0 20px !important}" in phone
    assert phone.index(".item .lone{padding-bottom:20px !important}") > phone.index(".item .txt{")


def test_the_leaving_section_says_what_happens_without_keep(env: Environment) -> None:
    text = html_to_text(env.get_template("newsletter.html").render(**_full_context()))

    assert "or it is deleted when its time is up." in text


def test_each_button_names_its_own_font(env: Environment) -> None:
    html = env.get_template("newsletter.html").render(**_full_context())

    buttons = re.findall(r'<td[^>]*>(?=<a[^>]* style="display:block)', html)
    assert len(buttons) == 5
    assert [b for b in buttons if "font-family:" not in b] == []
