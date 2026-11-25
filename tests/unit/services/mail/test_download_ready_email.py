"""Layout of the download-ready email, and its plain-text part."""

from __future__ import annotations

import pytest

from mediaman.services.downloads._notification_email import get_notification_template
from mediaman.services.mail.plaintext import html_to_text


def _render(**overrides: object) -> str:
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
    return get_notification_template().render(**ctx)


def test_a_long_unbroken_title_and_description_may_break_instead_of_widening_the_message() -> None:
    html = _render(title="Documentary_Extended_Cut_2160p_Remux", description="x" * 120)

    for text in ("Documentary_Extended_Cut_2160p_Remux", "x" * 120):
        tag = html[: html.rindex(text)].rsplit("<div", 1)[1]
        assert "overflow-wrap:anywhere" in tag


def test_a_phone_gets_narrower_side_padding() -> None:
    html = _render()

    phone = html.split("@media (max-width:520px){")[1].split("</style>")[0]
    assert ".pad{padding-left:20px !important;padding-right:20px !important}" in phone
    assert 'class="pad"' in html


def test_the_plain_text_part_says_what_the_mail_says() -> None:
    text = html_to_text(
        _render(
            title="The Salt Road",
            meta={"year": "2025", "media_label": "Movie", "runtime": "134", "director": "Li Ma"},
            ratings={"rating": "7.9", "imdb_rating": "7.8", "rt_rating": "91%"},
            description="A map-maker leaves the coast.",
            watch_url="https://mm.example/watch#title=tmdb-movie-1",
        )
    )

    assert [line for line in text.splitlines() if line] == [
        "The Salt Road is in the library. Press play when you are.",
        "mediaman",
        "✓ Ready to watch",
        "The Salt Road",
        "2025 · Movie · 2 h 14 min",
        "★ 7.9 IMDb 7.8 Rotten Tomatoes 91%",
        "A map-maker leaves the coast.",
        "Directed by Li Ma",
        "Watch now (https://mm.example/watch#title=tmdb-movie-1)",
        "You asked for this on mediaman, so we told you once it was there.",
    ]


def test_with_no_address_to_open_there_is_no_button() -> None:
    assert "<a " not in _render()


@pytest.mark.parametrize(
    ("minutes", "shown"),
    [("45", "Movie · 45 min"), ("120", "Movie · 2 h"), ("61", "Movie · 1 h 1 min"), ("", "Movie")],
)
def test_the_length_is_in_hours_and_minutes(minutes: str, shown: str) -> None:
    meta = {"year": "", "media_label": "Movie", "runtime": minutes, "director": ""}

    assert f"\n{shown}\n" in html_to_text(_render(meta=meta))


def test_a_show_is_created_by_its_creator_not_directed() -> None:
    meta = {"year": "", "media_label": "TV", "runtime": "", "director": "Li Ma"}

    text = html_to_text(_render(meta=meta))

    assert "Created by Li Ma" in text
    assert "Directed" not in text


def test_the_button_is_a_thumb_tall_and_drawn_by_its_cell() -> None:
    html = _render(watch_url="https://mm.example/watch")

    cell, link = html.split('<a href="https://mm.example/watch"')
    assert "background:#0071e3" in cell.rsplit("<td", 1)[1]
    assert "padding:13px " in link and "line-height:20px" in link
