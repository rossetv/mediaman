"""The Watch browse pages render what their context says, and stay CSP-safe."""

from __future__ import annotations

import itertools
import re
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import jinja2
import pytest

TEMPLATES = Path(__file__).resolve().parents[3] / "src/mediaman/web/templates"
CSS = Path(__file__).resolve().parents[3] / "src/mediaman/web/static/css"

# The nav belongs to another page's concerns; one with only the places the Watch page
# fills keeps these tests about the Watch templates.
_NAV = "{% block nav_center %}{% endblock %}{% block nav_end %}{% endblock %}"
_ENV = jinja2.Environment(
    loader=jinja2.ChoiceLoader(
        [jinja2.DictLoader({"_nav.html": _NAV}), jinja2.FileSystemLoader(TEMPLATES)]
    ),
    autoescape=True,
)


def _render(name: str, **ctx: object) -> str:
    return _ENV.get_template(name).render(**ctx)


def _card(**over: object) -> dict:
    card = {
        "kind": "movie",
        "title": "Brightwater",
        "subtitle": "Movie · 2019",
        "poster_url": "/api/poster/1",
        "href": "/watch/movie/1",
        "progress_pct": None,
        "minutes_left": None,
        "watched": False,
        "leaving_days": None,
        "keep_id": None,
    }
    card.update(over)
    return card


def _watch(**over: object) -> str:
    ctx: dict = {
        "banner": None,
        "rows": [],
        "watch_error": None,
        "is_admin": False,
        "view": "rows",
        "q": "",
        "tmdb_configured": False,
        "picks_on": False,
        "list_html": None,
        "page_data": {},
    }
    ctx.update(over)
    return _render("watch.html", **ctx)


def _show_ctx(**over: object) -> dict:
    ctx: dict = {
        "show": {
            "title": "The Long Coast",
            "meta_line": "TV · 2021",
            "summary": "A town.",
            "poster_url": "",
            "watched": False,
            "mark_kind": "show",
            "mark_key": "300",
        },
        "primary": {"label": "Resume S2 · E4", "href": "/watch/play/9?resume=1"},
        "seasons": [
            {
                "number": 1,
                "key": "301",
                "watched": True,
                "href": "/watch/show/s?season=1",
                "selected": False,
            },
            {
                "number": 2,
                "key": "302",
                "watched": False,
                "href": "/watch/show/s?season=2",
                "selected": True,
            },
        ],
        "leaving": None,
        "episodes": [
            {
                "number": 1,
                "title": "Spring Tide",
                "minutes": 52,
                "thumb_url": "/api/watch/thumb/1",
                "key": "1",
                "state": "watched",
                "watched": True,
                "progress_pct": None,
                "minutes_left": None,
                "href": "/watch/play/1",
            },
            {
                "number": 2,
                "title": "Low Water",
                "minutes": 50,
                "thumb_url": None,
                "key": "2",
                "state": "in_progress",
                "watched": False,
                "progress_pct": 38,
                "minutes_left": 31,
                "href": "/watch/play/2",
            },
            {
                "number": 3,
                "title": "Maydays",
                "minutes": 47,
                "thumb_url": None,
                "key": "3",
                "state": "none",
                "watched": False,
                "progress_pct": None,
                "minutes_left": None,
                "href": "/watch/play/3",
            },
        ],
        "back_href": "/watch",
        "watch_error": None,
    }
    ctx.update(over)
    return ctx


def _movie_ctx(**over: object) -> dict:
    ctx: dict = {
        "movie": {
            "title": "Fieldwork",
            "year": 2021,
            "minutes": 89,
            "summary": "A film.",
            "poster_url": "",
            "watched": True,
            "mark_kind": "movie",
            "mark_key": "5",
        },
        "primary": {"label": "Resume (31 min left)", "href": "/watch/play/5?resume=1"},
        "leaving": None,
        "back_href": "/watch",
        "watch_error": None,
    }
    ctx.update(over)
    return ctx


class _Inspect(HTMLParser):
    """Collect the markup facts the CSP cares about."""

    def __init__(self) -> None:
        super().__init__()
        self.inline_scripts = 0
        self.style_attrs = 0
        self.style_tags = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script" and "src" not in a and a.get("type") != "application/json":
            self.inline_scripts += 1
        if tag == "style":
            self.style_tags += 1
        if "style" in a:
            self.style_attrs += 1


def test_rows_render_and_an_empty_row_is_left_out() -> None:
    html = _watch(
        rows=[
            {"title": "Recently added", "items": [_card(), _card(title="Fieldwork")]},
            {"title": "Anime", "items": []},
        ]
    )
    assert "Recently added" in html
    assert html.count('class="watch-card"') == 2
    assert "Anime" not in html


class _Scrollers(HTMLParser):
    """Collect the attributes of every row scroller."""

    def __init__(self) -> None:
        super().__init__()
        self.found: list[dict] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "watch-row__scroll" in (a.get("class") or "").split():
            self.found.append(a)


def test_each_row_is_a_keyboard_reachable_region_named_after_its_title() -> None:
    html = _watch(
        rows=[
            {"title": "Recently added", "items": [_card()]},
            {"title": "Leaving soon", "items": [_card(leaving_days=2, keep_id="m1")]},
        ]
    )
    scrollers = _Scrollers()
    scrollers.feed(html)

    assert [(s.get("role"), s.get("tabindex"), s.get("aria-label")) for s in scrollers.found] == [
        ("region", "0", "Recently added"),
        ("region", "0", "Leaving soon"),
    ]


def test_watch_page_loads_the_row_script_from_a_file() -> None:
    assert '<script src="/static/js/watch-rows.js?v=" defer></script>' in _watch()


@pytest.mark.parametrize(
    ("days", "text"),
    [
        (-3, "Leaving any day"),
        (0, "Leaving today"),
        (1, "Leaving in 1 day"),
        (5, "Leaving in 5 days"),
    ],
)
def test_leaving_badge_wording(days: int, text: str) -> None:
    html = _watch(
        rows=[{"title": "Leaving soon", "items": [_card(leaving_days=days, keep_id="m1")]}]
    )
    assert f">{text}<" in html


@pytest.mark.parametrize(
    ("days", "words"),
    [
        (-1, ["Season 2 leaves any day", "S2 leaves any day", "S2 · Any day", "S2 · Due"]),
        (0, ["Season 2 leaves today", "S2 leaves today", "S2 · Today", "S2 Today"]),
        (1, ["Season 2 leaves in 1 day", "S2 · 1 day left", "S2 · 1 day", "S2 · 1d"]),
        (5, ["Season 2 leaves in 5 days", "S2 · 5 days left", "S2 · 5 days", "S2 · 5d"]),
    ],
)
def test_a_shows_leaving_badge_is_worded_for_its_season(days: int, words: list[str]) -> None:
    card = _card(leaving_days=days, leaving_season=2, leaving_id="s2")
    html = _watch(rows=[{"title": "TV", "items": [card]}])
    long, short, tiny, least = words
    assert f">{long}<" in html and f">{short}<" in html and f'data-words="{tiny}"' in html
    assert f'data-words="{least}"' in html
    # Kept, its badge goes, but its own card offers no Keep.
    assert 'data-leaving-id="s2"' in html and 'data-keep-id="s2"' not in html


@pytest.mark.parametrize(("days", "words"), [(-3, "Any day"), (0, "Today")])
def test_a_badge_for_today_or_any_day_has_words_for_a_narrow_poster(days: int, words: str) -> None:
    html = _watch(rows=[{"title": "Leaving soon", "items": [_card(leaving_days=days)]}])
    assert (
        f'<span class="watch-badge__tiny" data-words="{words}" aria-hidden="true"></span><span class="watch-badge__short">'
        in html
    )


@pytest.mark.parametrize("days", [1, 5, 365])
def test_a_badge_for_a_day_or_more_keeps_its_days_left_on_the_narrowest_phones(days: int) -> None:
    html = _watch(rows=[{"title": "Leaving soon", "items": [_card(leaving_days=days)]}])
    assert "watch-badge__tiny" not in html


@pytest.mark.parametrize(("days", "now"), [(-3, True), (0, True), (1, True), (2, False)])
def test_only_what_leaves_by_tomorrow_stands_out(days: int, now: bool) -> None:
    html = _watch(rows=[{"title": "Leaving soon", "items": [_card(leaving_days=days)]}])
    assert "watch-badge--leaving" in html
    assert ("watch-badge--leaving-now" in html) is now


def test_keep_button_only_for_cards_with_a_keep_id() -> None:
    plain = _watch(rows=[{"title": "Movies", "items": [_card()]}])
    scheduled = _watch(
        rows=[{"title": "Leaving soon", "items": [_card(leaving_days=2, keep_id="m1")]}]
    )
    assert "data-watch-keep" not in plain
    assert 'data-watch-keep="m1"' in scheduled
    for days in ("7", "30", "90"):
        assert f'data-days="{days}"' in scheduled


def test_progress_and_watched_marks_follow_the_card() -> None:
    html = _watch(
        rows=[
            {"title": "Continue watching", "items": [_card(progress_pct=38, minutes_left=31)]},
            {"title": "Movies", "items": [_card(watched=True)]},
        ]
    )
    assert 'value="38"' in html
    assert "31 min left" in " ".join(_captions(html))
    assert ">Watched<" in html


def test_each_face_of_the_watched_button_holds_its_own_icon() -> None:
    html = _render("watch_show.html", **_show_ctx())
    for face in ("off", "on"):
        for inside in re.findall(rf'<span class="watch-mark-btn__{face}">(.*?)</span>', html):
            assert inside.count("<use") == 1
    assert html.count('class="watch-mark-btn__on"') == html.count('class="watch-mark-btn__off"') > 0


def test_banner_shows_episodes_button_only_when_given() -> None:
    banner = {
        "kicker": "Continue watching",
        "title": "The Long Coast",
        "line": "Season 2 · Episode 4",
        "progress_pct": 38,
        "primary": {"label": "Resume", "href": "/watch/play/9?resume=1"},
        "episodes_href": None,
        "backdrop_url": None,
    }
    without = _watch(banner=banner)
    with_eps = _watch(banner={**banner, "episodes_href": "/watch/show/s"})
    assert "Resume" in without and "Continue watching" in without
    assert ">Episodes<" not in without
    assert ">Episodes<" in with_eps


def test_banner_shows_the_label_it_is_given_and_none_when_it_has_none() -> None:
    banner = {
        "kicker": "From your library",
        "title": "Salt & Iron",
        "line": "Movie",
        "progress_pct": None,
        "primary": {"label": "Play", "href": "/watch/play/1"},
        "episodes_href": None,
        "backdrop_url": None,
    }
    html = _watch(banner=banner)
    assert '<span class="watch-banner__kicker">From your library</span>' in html
    assert "Recently added" not in html and "Continue watching" not in html
    assert "watch-banner__kicker" not in _watch(banner={**banner, "kicker": None})


def test_banner_poster_waits_for_the_backdrop_and_every_picture_may_fail() -> None:
    banner = {
        "title": "Salt & Iron",
        "line": "Movie",
        "progress_pct": None,
        "primary": {"label": "Play", "href": "/watch/play/1"},
        "episodes_href": None,
        "backdrop_url": "/api/poster/1?kind=art",
        "next_backdrop_url": "/api/poster/1?kind=art&source=tmdb",
        "poster_url": "/api/poster/1",
    }
    html = _watch(banner=banner)
    images = re.findall(r'<img class="watch-banner__[^>]*>', html)

    assert [re.search(r'src="([^"]*)"', i).group(1) for i in images] == [
        "/api/poster/1?kind=art",
        "/api/poster/1",
        "/api/poster/1",
    ]
    assert ['loading="lazy"' in i for i in images] == [False, True, True]
    assert all("data-broken-on-error" in i for i in images)
    # The backdrop is judged on its shape once it has loaded; the poster copies never are.
    assert ["data-wide-art" in i for i in images] == [True, False, False]
    # Only the backdrop has another picture to try first.
    assert ['data-next-src="/api/poster/1?kind=art&amp;source=tmdb"' in i for i in images] == [
        True,
        False,
        False,
    ]
    retrying = _declarations_of("_watch.css", ".watch-banner__bg--art[data-retrying]")
    assert "opacity: 0" in retrying
    css = (CSS / "_watch.css").read_text()
    for part in ("bg--fill", "poster"):
        assert f".watch-banner__bg--art:not([data-broken]) ~ .watch-banner__{part}" in css


def test_with_scripts_on_the_backdrop_waits_to_be_known_for_a_wide_picture() -> None:
    art = ".watch-banner__bg--art"
    scripted = _in_media("_watch.css", "(scripting: enabled)")

    assert "opacity: 0" in _declarations_in(scripted, f"{art}:not([data-fit])")
    # Only there: with scripts off nothing would ever mark it.
    assert (CSS / "_watch.css").read_text().count(f"{art}:not([data-fit])") == 1
    assert "transition: none" in _declarations_of("_watch.css", f'{art}[data-fit="at-once"]')


def test_not_set_up_links_to_settings_for_admins_only() -> None:
    admin = _watch(watch_error="not_set_up", is_admin=True)
    viewer = _watch(watch_error="not_set_up", is_admin=False)
    assert "Plex isn't set up yet." in admin and 'href="/settings"' in admin
    assert "Plex isn't set up yet." in viewer and "/settings" not in viewer


def test_unreachable_offers_try_again_and_no_browse_content() -> None:
    html = _watch(watch_error="unreachable", rows=[{"title": "Movies", "items": [_card()]}])
    assert "Can't reach Plex right now." in html
    assert "data-watch-retry" in html and "Try again" in html
    assert "watch-card" not in html


def test_plex_notice_sits_above_the_rows_and_keeps_them() -> None:
    html = _watch(plex_unreachable=True, rows=[{"title": "Movies", "items": [_card()]}])
    assert html.index("Can't reach Plex right now.") < html.index("Movies")
    assert "data-watch-retry" in html and "watch-card" in html and "watch-search" in html
    assert "Can't reach Plex" not in _watch(rows=[{"title": "Movies", "items": [_card()]}])


def test_titles_from_plex_are_escaped() -> None:
    html = _watch(rows=[{"title": "Movies", "items": [_card(title="<script>alert(1)</script>")]}])
    assert "<script>alert(1)" not in html
    assert "&lt;script&gt;alert(1)" in html


def test_show_page_marks_each_episode_state_and_the_selected_season() -> None:
    html = _render("watch_show.html", **_show_ctx())
    assert "‹ Back to Watch" in html
    assert html.count('class="watch-pill') == 2
    assert html.count('aria-current="page"') == 1
    assert 'href="/watch/show/s?season=2" aria-current="page"' in html
    assert ">Watched<" in html and ">In progress<" in html
    assert "50 min · 31 min left" in html
    assert 'value="38"' in html
    assert 'href="/watch/play/3"' in html


def test_show_page_leaving_line_has_keep() -> None:
    plain = _render("watch_show.html", **_show_ctx())
    scheduled = _render("watch_show.html", **_show_ctx(leaving={"days": 2, "keep_id": "s2"}))
    assert "Leaving in" not in plain
    assert "Leaving in 2 days" in scheduled and 'data-watch-keep="s2"' in scheduled


def test_movie_page_shows_facts_primary_and_leaving_line() -> None:
    html = _render("watch_movie.html", **_movie_ctx(leaving={"days": 1, "keep_id": "m9"}))
    assert "2021 · 1 h 29 min" in html
    assert "Resume (31 min left)" in html
    assert "Leaving in 1 day" in html and 'data-watch-keep="m9"' in html


@pytest.mark.parametrize(
    ("name", "ctx"),
    [
        ("watch_show.html", _show_ctx(show={**_show_ctx()["show"], "poster_url": "/api/poster/7"})),
        (
            "watch_movie.html",
            _movie_ctx(movie={**_movie_ctx()["movie"], "poster_url": "/api/poster/7"}),
        ),
    ],
)
def test_a_title_s_own_page_shows_its_wide_picture_and_its_poster_only_without_one(
    name: str, ctx: dict
) -> None:
    page = _Tree(_render(name, **ctx))
    pictures = [a for tag, a, _in in page.found if tag == "img" and "/api/poster/" in a["src"]]
    art, fill, poster = pictures
    assert "data-wide-art" in art and "data-broken-on-error" in art
    assert art["src"] == "/api/poster/7?kind=art"
    assert art["data-next-src"] == "/api/poster/7?kind=art&source=tmdb"
    for img in (fill, poster):
        assert (img["src"], img["loading"]) == ("/api/poster/7", "lazy")


def test_a_show_s_season_tabs_stand_with_its_episodes_and_not_on_the_picture() -> None:
    html = _render("watch_show.html", **_show_ctx())
    banner = html.split('<section class="watch-banner"', 1)[1].split("</section>", 1)[0]
    assert "watch-btn--play" in banner
    assert "watch-pill" not in banner
    # Outside what a season's tab swaps.
    assert "watch-pill" not in html.split('<section class="watch-episodes"', 1)[1]


def test_movie_page_not_available() -> None:
    html = _render("watch_movie.html", not_available=True, back_href="/watch")
    assert "No longer available" in html
    assert "‹ Back to Watch" in html


def test_pages_have_no_inline_script_or_style() -> None:
    banner = {
        "title": "T",
        "line": "L",
        "progress_pct": 50,
        "primary": {"label": "Play", "href": "/watch/play/1"},
        "episodes_href": "/watch/show/s",
        "backdrop_url": "/api/watch/thumb/1",
    }
    pages = [
        _watch(
            banner=banner,
            rows=[
                {
                    "title": "Leaving soon",
                    "items": [_card(leaving_days=2, keep_id="m1", progress_pct=40, watched=True)],
                }
            ],
        ),
        _watch(watch_error="unreachable"),
        _render("watch_show.html", **_show_ctx(leaving={"days": 2, "keep_id": "s2"})),
        _render("watch_movie.html", **_movie_ctx(leaving={"days": 2, "keep_id": "m1"})),
        _render("watch_movie.html", not_available=True),
    ]
    for html in pages:
        found = _Inspect()
        found.feed(html)
        assert (found.inline_scripts, found.style_attrs, found.style_tags) == (0, 0, 0)
        assert 'src="/static/js/watch.js?v="' in html


def test_base_links_the_watch_and_player_styles() -> None:
    html = _watch()
    assert "/static/css/_watch.css" in html
    assert "/static/css/_player.css" in html


def _captions(html: str) -> list[str]:
    """What each card's caption says to anyone who reads it out, whatever is drawn of it."""
    found = re.findall(r'<span class="watch-card__sub">(.*?)</span>\s*</a>', html, re.S)
    return [unescape(re.sub(r"<[^>]+>", "", caption)) for caption in found]


@pytest.mark.parametrize("minutes", [60, 120, 180])
@pytest.mark.parametrize(("kind", "lead"), [("movie", "Movie"), ("show", "S2 · E4")])
def test_exact_hour_time_left_is_said_once(minutes, kind, lead):
    hours = f"{minutes // 60} h"
    item = _card(kind=kind, subtitle=f"{lead} · {hours} left", minutes_left=minutes)

    html = _watch(rows=[{"title": "Continue watching", "items": [item]}])

    assert _captions(html) == [f"{lead} · {hours} left"]


def test_card_without_time_in_its_caption_gets_it_appended():
    item = _card(subtitle="Movie", minutes_left=120)

    html = _watch(rows=[{"title": "Continue watching", "items": [item]}])

    assert _captions(html) == ["Movie · 2 h left"]


def test_leaving_badge_has_a_short_wording():
    html = _watch(rows=[{"title": "Leaving soon", "items": [_card(leaving_days=90, keep_id="k")]}])

    assert "Leaving in 90 days" in html
    assert "90 days left" in html


def test_banner_carries_a_short_line_for_phones():
    banner = {
        "title": "The Long Coast",
        "line": 'Season 2 · Episode 4 — "Low Water" · 31 min left',
        "line_short": "S2 · E4 · 31 min left",
        "progress_pct": 40,
        "primary": {"label": "Resume", "href": "/watch/play/9"},
        "episodes_href": None,
        "backdrop_url": None,
    }

    html = _watch(banner=banner)

    assert "S2 · E4 · 31 min left" in html
    assert "Season 2 · Episode 4" in html


def test_keep_shows_only_in_the_leaving_soon_row():
    item = _card(leaving_days=3, keep_id="m1")

    html = _watch(
        rows=[
            {"title": "Leaving soon", "items": [item]},
            {"title": "Recently added", "items": [item]},
        ]
    )

    assert html.count('data-watch-keep="m1"') == 1
    assert html.count("Leaving in 3 days") == 2
    leaving_row, other_row = html.split('aria-label="Recently added"')
    assert 'data-watch-keep="m1"' in leaving_row
    assert 'data-keep-id="m1"' in other_row


def test_the_keeps_of_two_seasons_of_a_show_are_told_apart_by_name():
    seasons = [
        _card(
            kind="season",
            title="Glass Coast",
            subtitle=f"Season {n}",
            leaving_days=3,
            keep_id=f"s{n}",
        )
        for n in (2, 3)
    ]

    html = _watch(rows=[{"title": "Leaving soon", "items": seasons}])

    labels = re.findall(r'data-watch-keep="(\w+)"[^>]*aria-label="([^"]*)"', html)
    assert labels == [("s2", "Keep Glass Coast, Season 2"), ("s3", "Keep Glass Coast, Season 3")]


def test_a_films_keep_is_named_by_its_title_alone():
    item = _card(kind="movie", title="Fieldwork", subtitle="Movie", leaving_days=3, keep_id="m1")

    html = _watch(rows=[{"title": "Leaving soon", "items": [item]}])

    assert 'aria-label="Keep Fieldwork"' in html


def test_search_has_a_clear_button_and_announces_outside_the_hidden_results():
    html = _watch()

    assert 'id="watch-search-clear"' in html
    top, results = html.split('id="watch-results"')
    assert 'id="watch-search-status" role="status"' in top
    # The title overlay, after the results, has a status line of its own.
    results, overlay = results.split('id="ttl"')
    assert 'role="status"' not in results
    assert "watch-search-status" not in overlay


def test_a_titles_page_does_not_hide_the_bar_of_a_tv_from_a_screen_reader():
    tag = re.search(r'<div class="ttl"[^>]*>', _watch())
    assert tag is not None
    assert 'role="dialog"' in tag.group(0)
    assert "aria-modal" not in tag.group(0)


def test_a_phone_s_keyboard_offers_search_and_leaves_what_is_typed_alone() -> None:
    field = re.search(r'<input id="watch-search"[^>]*>', _watch())
    assert field is not None
    for attr in ('enterkeyhint="search"', 'autocapitalize="off"', 'autocorrect="off"'):
        assert attr in field.group(0)


@pytest.mark.parametrize(
    "page", [("watch_show.html", _show_ctx()), ("watch_movie.html", _movie_ctx())]
)
def test_title_pages_load_the_poster_fade_script(page):
    name, ctx = page

    assert 'src="/static/js/watch-rows.js?v="' in _render(name, **ctx)


def test_a_title_page_past_its_date_says_any_day() -> None:
    html = _render("watch_movie.html", **_movie_ctx(leaving={"days": -3, "keep_id": "m9"}))
    assert '<span class="watch-leaving__text">Leaving any day</span>' in html


@pytest.mark.parametrize(
    ("days", "text"),
    [
        (-3, "Season 1 leaves any day"),
        (0, "Season 1 leaves today"),
        (1, "Season 1 leaves in 1 day"),
        (5, "Season 1 leaves in 5 days"),
    ],
)
def test_show_page_names_another_leaving_season_without_a_keep(days: int, text: str) -> None:
    html = _render(
        "watch_show.html",
        **_show_ctx(season_leaving=True, other_leaving={"season": 1, "days": days}),
    )
    assert text in html
    assert "watch-leaving--other" in html
    assert "data-watch-keep" not in html


def test_discover_skeleton_puts_recommended_first_with_its_tools_height() -> None:
    html = _watch(tmdb_configured=True, picks_on=True)
    skeleton = html.split('class="watch-discover-skel"', 1)[1].split('id="watch-results"', 1)[0]
    rows = skeleton.split('class="watch-row watch-row--skel')[1:]
    assert len(rows) == 4
    assert rows[0].startswith(" watch-row--skel-tools")
    assert not any("skel-tools" in r for r in rows[1:])
    assert "watch-discover-skel" not in _watch()


def test_banner_details_opens_the_title_overlay() -> None:
    banner = {
        "title": "The Long Coast",
        "ref": "show-500",
        "line": "",
        "progress_pct": None,
        "primary": {"label": "Play", "href": "/watch/play/9"},
        "episodes_href": "/watch/show/500",
        "backdrop_url": None,
    }
    html = _watch(banner=banner)
    actions = html.split('class="watch-actions"', 1)[1].split("</div>", 1)[0]
    details = re.search(r'<a [^>]*data-title-ref="show-500"[^>]*>(?=Details</a>)', actions)
    assert details is not None
    assert 'href="/watch#title=show-500"' in details.group(0)
    assert 'data-title="The Long Coast"' in details.group(0)
    assert "watch-btn--grey" in details.group(0)
    assert "Details</a>" in actions


def _phone_rules(sheet: str) -> list[tuple[str, str]]:
    """(selectors, declarations) of every rule inside a `@media (max-width: 699.98px)` block."""
    css = re.sub(r"/\*.*?\*/", "", (CSS / sheet).read_text(encoding="utf-8"), flags=re.S)
    blocks = re.findall(r"@media \(max-width: 699\.98px\) \{((?:[^{}]*\{[^{}]*\})*)\s*\}", css)
    return [
        (head.strip(), body)
        for block in blocks
        for head, body in re.findall(r"([^{}]+)\{([^{}]*)\}", block)
    ]


def test_banner_episodes_opens_the_title_overlay_at_its_episodes() -> None:
    html = _watch(
        banner={**_BANNER, "ref": "show-500", "episodes_href": "/watch/show/500?season=2"}
    )
    link = re.search(r"<a [^>]*>Episodes</a>", html)
    assert link is not None
    assert 'href="/watch/show/500?season=2"' in link.group(0)
    assert 'data-title-ref="show-500"' in link.group(0)
    assert 'data-title-at="episodes"' in link.group(0)


def test_the_picks_batch_and_refresh_tools_are_44px_tall_below_700px() -> None:
    rules = _phone_rules("_watch_browse.css")
    assert any(
        head == ".row-tool" and re.search(r"(?<![\w-])height:\s*44px", body) for head, body in rules
    )
    # The skeleton's Recommended heading matches the 44px tools, so nothing moves when they load.
    skel = [
        body
        for head, body in rules
        if head.endswith(".watch-row--skel-tools .watch-row__title-skel")
    ]
    assert skel and "44px" in skel[0]


def test_the_icon_only_row_tool_is_44px_wide_below_700px() -> None:
    # Below 600px the long label drops away and only the icon is left: padding alone is 35px.
    rules = _phone_rules("_watch_browse.css")
    assert any(
        head == ".row-tool" and re.search(r"(?<![\w-])min-width:\s*44px", body)
        for head, body in rules
    )


def _declarations_of(sheet: str, selector: str) -> str:
    css = re.sub(r"/\*.*?\*/", "", (CSS / sheet).read_text(encoding="utf-8"), flags=re.S)
    return "".join(
        body
        for head, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
        if selector in [s.strip() for s in head.split(",")]
    )


@pytest.mark.parametrize("selector", [".ttl-rating--rt", ".ttl__error"])
def test_the_title_overlay_sets_red_text_on_the_one_red_text_token(selector: str) -> None:
    # A pink --danger-soft beside the coral leaving line made three reds on one panel.
    body = _declarations_of("_watch_detail.css", selector)
    assert re.search(r"(?<![\w-])color:\s*var\(--danger-text\)", body), body
    assert "danger-soft" not in (CSS / "_watch_detail.css").read_text(encoding="utf-8")


def test_share_centres_its_icon_and_label_together_inside_the_width_it_holds() -> None:
    sheet = "_watch_detail.css"
    button = _declarations_of(sheet, ".ttl__share")
    assert "display: inline-grid" in button and "place-items: center" in button
    # The unseen longest label and the group share one grid cell, so the cell is as wide as the
    # longest state and the group sits in the middle of it.
    group = _declarations_of(sheet, ".ttl__share-group")
    sizer = _declarations_of(sheet, ".ttl__share::after")
    assert "grid-area: 1 / 1" in group and "grid-area: 1 / 1" in sizer
    assert "visibility: hidden" in sizer and "content: attr(data-widest)" in sizer
    # The sizer is as wide as the icon and the gap before the label, which the group also has.
    assert "padding-inline-start: calc(var(--share-icon) + var(--share-gap))" in sizer
    assert "gap: var(--share-gap)" in group
    assert "width: var(--share-icon)" in _declarations_of(sheet, ".ttl__share-group > .ico")


def test_the_banner_grows_with_the_screen_and_with_a_long_title() -> None:
    body = _declarations_of("_watch.css", ".watch-banner")

    assert "min-height: clamp(420px, 27vw, 600px)" in body
    assert not re.search(r"(?<!-)\bheight: \d+px", body)


def test_the_banner_is_short_on_a_phone_held_sideways() -> None:
    css = (CSS / "_watch.css").read_text(encoding="utf-8")
    block = re.search(r"@media \(max-height: 500px\)[^{]*\{((?:[^{}]*\{[^{}]*\})*)\s*\}", css)

    assert block and "min-height: 260px" in block.group(1)


def test_a_list_poster_of_any_shape_fills_its_frame() -> None:
    body = _declarations_of("_tables.css", ".thumb img")

    assert "width: 100%" in body and "height: 100%" in body and "object-fit: cover" in body


def test_the_list_actions_column_hugs_its_buttons() -> None:
    assert "width: 1%" in _declarations_of("_watch_list.css", ".wl-table .col-act")


def test_the_dash_of_an_unscheduled_title_is_hidden_on_phone_cards() -> None:
    rules = dict(_phone_rules("_watch_list.css"))

    assert "display: none" in rules[".wl-del--none > [aria-hidden]"]


@pytest.mark.parametrize("place", [".watch-card__poster", ".watch-ep__thumb"])
def test_a_progress_bar_sits_inside_the_rounded_corners(place: str) -> None:
    body = _declarations_of("_watch.css", f"{place} > .watch-bar")

    assert "left: 8px" in body and "bottom: 8px" in body and "calc(100% - 16px)" in body


@pytest.mark.parametrize("part", ["::-webkit-progress-value", "::-moz-progress-bar"])
def test_a_little_progress_still_shows_a_mark_and_none_shows_nothing(part: str) -> None:
    assert "min-width: 6px" in _declarations_of("_watch.css", f'.watch-bar:not([value="0"]){part}')
    assert "min-width" not in _declarations_of("_watch.css", f".watch-bar{part}")


def _in_media(sheet: str, query: str) -> str:
    """The rules inside every `@media` block whose query holds *query*."""
    css = re.sub(r"/\*.*?\*/", "", (CSS / sheet).read_text(encoding="utf-8"), flags=re.S)
    blocks = re.findall(r"@media ([^{]*)\{((?:[^{}]*\{[^{}]*\})*)\s*\}", css)
    return "".join(body for head, body in blocks if query in head)


def _declarations_in(rules: str, selector: str) -> str:
    return "".join(
        body
        for head, body in re.findall(r"([^{}]+)\{([^{}]*)\}", rules)
        if selector in [s.strip() for s in head.split(",")]
    )


@pytest.mark.parametrize(
    ("sheet", "poster"),
    [
        ("_watch.css", ".watch-banner__poster"),
        ("_watch_detail.css", ".ttl__poster"),
        ("_download_page.css", ".dl-cinema__poster"),
    ],
)
def test_a_poster_standing_in_for_a_backdrop_keeps_its_own_shape(sheet: str, poster: str) -> None:
    body = _declarations_of(sheet, poster)

    assert "aspect-ratio: auto 2 / 3" in body and "height: auto" in body
    assert "object-fit: cover" not in body and "filter" not in body


def test_the_banner_poster_keeps_clear_of_the_banner_edges() -> None:
    assert "max-height: calc(100% - 72px)" in _declarations_of(
        "_watch.css", ".watch-banner__poster"
    )


def test_a_long_banner_title_is_smaller_and_every_title_clamps() -> None:
    banner = {
        "title": "It",
        "line": "Movie",
        "progress_pct": None,
        "primary": {"label": "Play", "href": "/watch/play/1"},
        "episodes_href": None,
    }
    long = "An Improbable Chronicle of the Winter Orchard and the Ferryman"

    assert "watch-banner__title--long" not in _watch(banner=banner)
    assert "watch-banner__title--long" in _watch(banner={**banner, "title": long})
    smaller = ".watch-banner__title--long"
    assert "font-size: 36px" in _declarations_of("_watch.css", smaller)
    # 80 characters, all capitals, beside the poster's place at 600 wide.
    assert "--title-lines: 7" in _declarations_of("_watch.css", smaller)
    for query, lines in (("(max-width: 599.98px)", 6), ("(max-height: 500px)", 4)):
        rules = _declarations_in(_in_media("_watch.css", query), smaller)
        assert f"--title-lines: {lines}" in rules
    assert "line-clamp: var(--title-lines)" in _declarations_of(
        "_watch.css", ".watch-banner__title"
    )
    phone = _declarations_in(
        _in_media("_watch.css", "(max-width: 599.98px)"), ".watch-banner__title"
    )
    assert "--title-lines: 4" in phone


def test_a_very_long_banner_title_gets_the_smallest_text_and_lines_enough_to_fit() -> None:
    banner = {
        "title": "Dr. Aldermoor's Improbable Chronicle of the Winter Orchard, the Ferryman, "
        "and Everything That Happened After the Lanterns Went Out: Part Two",
        "line": "Movie",
        "progress_pct": None,
        "primary": {"label": "Play", "href": "/watch/play/1"},
        "episodes_href": None,
    }
    very_long = ".watch-banner__title--longest"

    assert f"watch-banner__title {very_long[1:]}" in _watch(banner=banner)
    sideways = _in_media("_watch.css", "(max-height: 500px)")
    assert "--title-lines: 8" in _declarations_of("_watch.css", very_long)
    assert "--title-lines: 8" in _declarations_in(
        _in_media("_watch.css", "(max-width: 599.98px)"), very_long
    )
    assert "--title-lines: 5" in _declarations_in(sideways, very_long)


def test_the_shade_on_a_small_title_hero_keeps_close_to_the_text() -> None:
    small = _declarations_in(
        _in_media("_watch_detail.css", "(max-width: 639.98px)"), ".ttl__head::before"
    )

    assert "inset-block: -40px" in small
    assert "55%, transparent) calc(100% - 40px)" in small


def test_a_title_with_no_picture_has_a_short_hero_until_its_trailer_plays() -> None:
    short = _declarations_of("_watch_detail.css", ".ttl.is-short:not(.is-trailer) .ttl__hero")
    bare = ".ttl__hero:has(> .ttl__hero-skel[hidden]):not(:has(> img:not([hidden])))"

    assert "min-height: 190px" in short
    assert "var(--hero-hi)" in _declarations_of("_watch_detail.css", bare)
    phone = _in_media("_watch_detail.css", "(max-width: 699.98px)")
    assert "display: none" in _declarations_in(phone, ".ttl.is-late .ttl__poster")


def test_the_certificate_sits_8px_after_the_text_before_it() -> None:
    body = _declarations_of("_watch_detail.css", ".ttl__meta > span:has(+ .ttl__cert)")

    assert "margin-right: calc(8px - var(--dot))" in body


def test_a_detail_that_wraps_stays_clear_of_the_clipped_dot_margin() -> None:
    sheet = "_watch_detail.css"

    assert "padding-left: var(--dot)" in _declarations_of(sheet, ".ttl__meta > span")
    assert "position: absolute" in _declarations_of(sheet, ".ttl__meta > span::before")


def test_the_banner_scrim_does_not_widen_with_a_wide_screen() -> None:
    wide = _declarations_in(_in_media("_watch.css", "(min-width: 1280px)"), ".watch-banner")

    stops = re.findall(r"(\d+)px", wide)
    assert stops[0] == "320" and stops[-1] == "1150"


def test_a_card_progress_bar_lines_up_with_the_title_above_it() -> None:
    assert "padding: var(--card-pad)" in _declarations_of("_watch.css", ".watch-card__poster")
    assert "left: var(--card-pad)" in _declarations_of(
        "_watch.css", ".watch-card__poster > .watch-bar"
    )


def test_the_shade_behind_a_title_over_a_progress_bar_starts_higher_and_ends_darker() -> None:
    body = _declarations_of("_watch.css", ".watch-card__poster:has(> .watch-bar)::after")

    assert "transparent 35%" in body and "var(--scrim) 92%" in body


def test_the_phone_banner_bar_is_as_wide_as_the_text() -> None:
    phone = _in_media("_watch.css", "(max-width: 599.98px)")

    assert "width: 100%" in _declarations_in(phone, ".watch-bar--banner")


def test_a_narrow_phone_held_sideways_puts_the_shade_and_the_buttons_beside_the_text() -> None:
    sideways = _in_media("_watch.css", "(max-width: 699.98px) and (max-height: 500px)")

    assert "var(--banner-side)" in _declarations_in(sideways, ".watch-banner::after")
    assert "min-height: 0" in _declarations_in(sideways, ".watch-banner")
    assert "var(--banner-text-side)" in _declarations_in(sideways, ".watch-banner__body::before")
    # At the top of their own column, so a long title never pushes them off the screen.
    actions = _declarations_in(sideways, ".watch-banner .watch-actions")
    assert "grid-column: 2" in actions and "align-self: start" in actions


def test_forced_colors_drop_the_pictures_and_keep_progress_visible() -> None:
    forced = _in_media("_watch.css", "forced-colors: active")

    assert "display: none" in _declarations_in(forced, ".watch-banner__bg")
    bar = _declarations_in(forced, "progress.watch-bar")
    assert "forced-color-adjust: none" in bar and "CanvasText" in bar
    for part in ("::-webkit-progress-value", "::-moz-progress-bar"):
        assert "Highlight" in _declarations_in(forced, f"progress.watch-bar{part}")
    overlay = _in_media("_watch_detail.css", "forced-colors: active")
    assert "display: none" in _declarations_in(overlay, ".ttl__art")


def test_a_search_in_the_address_opens_the_page_on_its_results() -> None:
    def open_panels(html: str) -> tuple[bool, bool]:
        browse = re.search(r'<div id="watch-browse"([^>]*)>', html)
        results = re.search(r'<section class="watch-results"[^>]*>', html)
        assert browse and results
        return "hidden" in browse.group(1), "hidden" in results.group(0)

    assert open_panels(_watch(q="orchard")) == (True, False)
    assert open_panels(_watch(q="")) == (False, True)
    for key in ("genre", "person"):
        asked = SimpleNamespace(query_params={key: "28"})
        assert open_panels(_watch(request=asked)) == (True, False)


def test_the_watch_scripts_load_in_the_order_they_need_each_other() -> None:
    scripts = re.findall(r'src="/static/js/(watch[^"?]*)\.js', _watch())
    place = {name: n for n, name in enumerate(scripts)}

    assert scripts[0] == "watch/core"
    assert place["watch/address"] == 1
    for first, then in [
        ("keep", "search"),
        ("search", "browse"),
        ("rows-load", "grid"),
        ("grid", "explore"),
        ("search", "explore"),
        ("collection", "detail"),
    ]:
        assert place[f"watch/{first}"] < place[f"watch/{then}"]
    # It starts them all and opens what the address names.
    assert place["watch"] > place["watch/explore"]
    html = _watch()
    assert html.index("/static/js/ratings.js") < html.index("/static/js/watch/marks.js")


@pytest.mark.parametrize(
    ("admin", "tmdb", "says", "never"),
    [
        (True, True, "search above for something to download", "Connect TMDB"),
        (True, False, "Connect TMDB", "search above"),
        (False, True, "ask whoever runs mediaman", "Settings"),
        (False, False, "ask whoever runs mediaman", "TMDB"),
    ],
)
def test_an_empty_library_says_so_and_what_to_do_next(
    admin: bool, tmdb: bool, says: str, never: str
) -> None:
    html = _watch(is_admin=admin, tmdb_configured=tmdb)
    empty = re.search(r'<section class="watch-empty">(.*?)</section>', html, re.S)

    assert empty
    text = empty.group(1)
    assert "Nothing to watch yet" in text
    assert says.lower() in text.lower()
    assert never.lower() not in text.lower()
    assert ('href="/settings"' in text) is admin


def test_a_library_with_titles_or_a_plex_error_has_no_empty_state() -> None:
    row = {"title": "Movies", "items": [_card()], "library": True}

    assert "watch-empty" not in _watch(rows=[row])
    assert "watch-empty" not in _watch(watch_error="not_set_up")


def _scrim_at(sheet: str, selector: str, position: float) -> float:
    """The scrim alpha (%) of the selector's gradient at *position* % of its length."""
    body = _declarations_of(sheet, selector)
    found = re.findall(r"(?:--scrim\) (\d+)%, )?transparent\)? (\d+)%", body)
    stops = [(float(pos), float(alpha or 0)) for alpha, pos in found]
    for (p0, a0), (p1, a1) in itertools.pairwise(stops):
        if p0 <= position <= p1:
            return a0 + (a1 - a0) * (position - p0) / (p1 - p0)
    raise AssertionError(stops)


def test_a_four_line_card_title_sits_on_at_least_half_dark() -> None:
    # Four lines of 14px reach a third of the way up a poster; white on a pale one needs about half.
    assert _scrim_at("_watch.css", ".watch-card__poster::after", 67) >= 50


def test_the_hint_under_an_empty_search_is_centred() -> None:
    body = _declarations_of("_watch_search.css", ".results-empty + .results-hint")

    assert "margin: 12px auto 0" in body and "justify-content: center" in body


def test_results_that_fade_out_keep_their_columns() -> None:
    # They leave from outside .watch-results.
    assert "repeat(3," in _declarations_of("_watch_search.css", ".results-page .watch-grid")


class _Tree(HTMLParser):
    """Every element's tag and attributes, each with the tags it stands in."""

    def __init__(self, html: str) -> None:
        super().__init__()
        self.found: list[tuple[str, dict, list[str]]] = []
        self._open: list[str] = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.found.append((tag, dict(attrs), list(self._open)))
        if tag not in ("img", "input", "br", "hr", "meta", "link", "use", "path"):
            self._open.append(tag)

    def handle_endtag(self, tag):
        if tag in self._open:
            while self._open.pop() != tag:
                pass

    def having(self, attr: str) -> list[dict]:
        return [a for _tag, a, _in in self.found if attr in a]


def _marked(**over: object) -> dict:
    return _card(mark_kind="movie", mark_key="1", dismiss_ref=None, **over)


_BANNER = {
    "kicker": "Continue watching",
    "title": "Salt & Iron",
    "ref": "movie-1",
    "line": "31 min left",
    "progress_pct": 40,
    "primary": {"label": "Resume", "href": "/watch/play/1"},
    "episodes_href": None,
    "backdrop_url": None,
    "dismiss_ref": "movie-1",
}


def test_a_card_has_one_more_button_named_after_it_only_when_it_can_be_marked() -> None:
    tree = _Tree(_watch(rows=[{"title": "Movies", "items": [_marked(), _card()]}]))
    [more] = tree.having("data-watch-more")
    assert (more["aria-label"], more["aria-haspopup"]) == ("More for Brightwater", "menu")
    assert [c.get("data-mark") for c in tree.having("data-keep-id")] == ["movie/1", None]


def test_a_watched_poster_says_so_in_words_and_not_with_the_tick_of_in_library() -> None:
    html = _watch(rows=[{"title": "Movies", "items": [_marked(watched=True)]}])
    badge = re.search(r'<span class="watch-badge watch-badge--watched">(.*?)</span></span>', html)
    assert badge
    assert '<span class="watch-badge__word">Watched' in badge.group(1)
    assert "#i-eye" in badge.group(1) and "#i-check" not in badge.group(1)


def test_dismiss_is_offered_by_a_continue_watching_card_and_its_banner_alone() -> None:
    item = {**_marked(), "dismiss_ref": "movie-1"}
    rows = [{"title": "Continue watching", "items": [item]}]
    tree = _Tree(_watch(banner=_BANNER, rows=rows))
    assert [a["data-dismiss-ref"] for a in tree.having("data-dismiss-ref")] == ["movie-1"] * 2
    [button] = tree.having("data-watch-dismiss")
    assert button["aria-label"] == "Dismiss Salt & Iron from Continue watching"

    other = _Tree(
        _watch(
            banner={**_BANNER, "dismiss_ref": None},
            rows=[{"title": "Movies", "items": [_marked()]}],
        )
    )
    assert not other.having("data-dismiss-ref") and not other.having("data-watch-dismiss")


def test_the_pages_toggles_say_what_is_watched_and_what_a_press_does() -> None:
    def toggles(html: str) -> list[tuple]:
        return [
            (a["data-mark-toggle"], a["aria-pressed"], a["aria-label"])
            for a in _Tree(html).having("data-mark-toggle")
        ]

    assert toggles(_render("watch_movie.html", **_movie_ctx())) == [
        ("movie/5", "true", "Mark as watched")
    ]
    assert toggles(_render("watch_show.html", **_show_ctx())) == [
        ("show/300", "false", "Mark show as watched"),
        ("season/302", "false", "Mark season as watched"),
        ("episode/1", "true", "Mark Spring Tide as watched"),
        ("episode/2", "false", "Mark Low Water as watched"),
        ("episode/3", "false", "Mark Maydays as watched"),
    ]


def test_a_watched_season_s_tab_says_so_without_changing_its_words() -> None:
    html = _render("watch_show.html", **_show_ctx())
    tabs = re.findall(r'<a class="watch-pill[^>]*>.*?</a>', html)
    assert 'aria-label="Season 1, watched"' in tabs[0] and "#i-eye" in tabs[0]
    assert "aria-label" not in tabs[1] and "#i-eye" not in tabs[1]
    # seasons.js reads the tab's text as the season's name.
    assert re.sub(r"<[^>]+>", "", tabs[0]) == "Season 1"


def test_an_episode_seen_before_says_so_while_it_is_played_again() -> None:
    ctx = _show_ctx()
    ctx["episodes"][1]["watched"] = True
    html = _render("watch_show.html", **ctx)
    assert 'Low Water<span class="sr-only">, watched</span>' in html
    assert ">In progress<" in html


def test_no_button_stands_in_a_link() -> None:
    pages = [
        _watch(
            banner=_BANNER,
            rows=[{"title": "Leaving soon", "items": [_marked(leaving_days=2, keep_id="m1")]}],
        ),
        _render("watch_show.html", **_show_ctx()),
        _render("watch_movie.html", **_movie_ctx()),
    ]
    for html in pages:
        tree = _Tree(html)
        assert any(tag == "button" for tag, _a, _in in tree.found)
        assert not [a for tag, a, around in tree.found if tag == "button" and "a" in around]


def test_a_long_library_row_draws_its_first_page_and_where_to_go_on() -> None:
    row = {
        "title": "Movies",
        "items": [_marked(title=f"Film {n}") for n in range(5)],
        "library": True,
        "kind": "movies",
        "page_size": 3,
        "next": "c1",
    }
    tree = _Tree(_watch(rows=[row]))
    assert len(tree.having("data-mark")) == 3
    [viewport] = tree.having("data-row-next")
    assert (viewport["data-row-kind"], viewport["data-row-next"]) == ("movies", "c1")

    whole = _Tree(_watch(rows=[{**row, "next": None}]))
    assert len(whole.having("data-mark")) == 5 and not whole.having("data-row-next")
