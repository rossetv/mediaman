"""What every mail must hold, whichever it is: text that reads at the AA contrast in a light and a
dark mail, colours Outlook can draw, no broken picture box, and buttons that name their own font."""

from __future__ import annotations

import re
import struct
from collections.abc import Callable, Iterator

import pytest
from jinja2 import Environment, FileSystemLoader

from mediaman.app_factory import _STATIC_DIR
from mediaman.services.mail.newsletter.render import _TEMPLATE_DIR
from tests.unit.services.mail.test_download_ready_email import _render as _ready
from tests.unit.services.mail.test_newsletter_template import _contrast, _full_context

_HEX = r"#[0-9a-f]{6}"
_PAGE = ("#ffffff", "#f2f2f4")  # the card, and the page around it
_ENV = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)), autoescape=True)


def _newsletter(pictures: bool = True) -> str:
    ctx = _full_context(dry_run=True)
    ctx["this_week_items"][0]["download_state"] = "queued"
    if not pictures:
        for group in ("scheduled_items", "deleted_items", "this_week_items"):
            for item in ctx[group]:
                item["poster_url"] = ""
    return _ENV.get_template("newsletter.html").render(**ctx)


def _ready_mail(pictures: bool = True) -> str:
    return _ready(
        title="The Salt Road",
        season_label="Season 2",
        poster_src="https://mm.example/p.jpg" if pictures else "",
        backdrop_src="https://mm.example/b.jpg" if pictures else "",
        meta={"year": "2025", "media_label": "Movie", "runtime": "134", "director": "Li Ma"},
        ratings={"rating": "7.9", "imdb_rating": "7.8", "rt_rating": "91%"},
        description="A map-maker leaves the coast.",
        watch_url="https://mm.example/watch",
        site="https://mm.example",
    )


MAILS: dict[str, Callable[..., str]] = {"newsletter": _newsletter, "ready": _ready_mail}
every_mail = pytest.mark.parametrize("mail", MAILS.values(), ids=MAILS.keys())


def _found(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text)
    return match.group(1) if match else None


def _texts(html: str) -> Iterator[tuple[list[str], str, str | None]]:
    """Each tag of the body that colours its text: its classes, its colour, and the plate under
    it (its own background, or its cell's, as a button's link has), or None on the page."""
    body = html.split("<body", 1)[1]
    for tag in re.finditer(r"<[a-z]+\b[^>]*>", body):
        style = _found(r'style="([^"]*)"', tag.group(0)) or ""
        text = _found(rf"(?<![\w-])color:({_HEX})", style)
        if not text or "display:none" in style:
            continue
        cell = body[body.rfind("<", 0, tag.start()) : tag.start()]
        plate = next(
            (
                _found(pattern, where)
                for where in (tag.group(0), cell if cell.startswith("<td") else "")
                for pattern in (rf"(?<![\w-])background:({_HEX})", rf'bgcolor="({_HEX})"')
                if _found(pattern, where)
            ),
            None,
        )
        yield (_found(r'class="([^"]*)"', tag.group(0)) or "").split(), text, plate


def _dark_rules(html: str) -> dict[str, str]:
    dark = re.search(r"@media \(prefers-color-scheme:dark\)\{(.*?)\n\}", html, re.S).group(1)
    return dict(re.findall(r"\.([\w-]+)\{([^}]*)\}", dark))


def _dark(rule: str, prop: str) -> str | None:
    return _found(rf"(?<![\w-]){prop}:({_HEX}) !important", rule)


@every_mail
def test_every_text_colour_is_solid_as_outlook_ignores_any_other(mail: Callable[..., str]) -> None:
    colours = re.findall(r"(?<![\w-])color:\s*([^;\"}!]+)", mail())

    assert colours
    assert [c for c in colours if not re.fullmatch(_HEX, c.strip())] == []


@every_mail
def test_all_text_reads_at_the_aa_contrast_without_the_styles(mail: Callable[..., str]) -> None:
    texts = list(_texts(mail()))

    assert any(plate for *_, plate in texts) and any(not plate for *_, plate in texts)
    for classes, text, plate in texts:
        if "man" in classes:
            continue  # the name, a logo, which no contrast rule covers
        for under in [plate] if plate else _PAGE:
            assert _contrast(text, under) >= 4.5, (classes, text, under)


@every_mail
def test_all_text_reads_at_the_aa_contrast_in_the_reader_s_dark_mail(
    mail: Callable[..., str],
) -> None:
    rules = _dark_rules(mail())

    for name, rule in rules.items():
        text = _dark(rule, "color")
        plates = [_dark(rule, "background")] if _dark(rule, "background") else ["card", "bg"]
        for plate in plates if text else []:
            under = plate if plate.startswith("#") else _dark(rules[plate], "background")
            assert _contrast(text, under) >= 4.5, (name, under)


@every_mail
def test_text_on_the_page_turns_light_in_the_reader_s_dark_mail(mail: Callable[..., str]) -> None:
    html = mail()
    rules = _dark_rules(html)

    for classes, text, plate in _texts(html):
        if plate is None or any(_dark(rules.get(c, ""), "background") for c in classes):
            assert any(_dark(rules.get(c, ""), "color") for c in classes), (classes, text)


@every_mail
def test_no_picture_leaves_no_image_tag(mail: Callable[..., str]) -> None:
    assert '<img class="pic"' in mail()
    assert '<img class="pic"' not in mail(pictures=False)


@every_mail
def test_each_button_names_its_own_font(mail: Callable[..., str]) -> None:
    buttons = re.findall(r'<td[^>]*>(?=<a[^>]* style="display:block)', mail())

    assert buttons
    assert [b for b in buttons if "font-family:" not in b] == []


def _name_picture(html: str) -> str | None:
    """The tag of the picture of the name in the mail's head, if it has one."""
    return _found(r'(<img [^>]*alt="mediaman"[^>]*>)', html)


@every_mail
def test_the_head_draws_the_apps_name_as_a_picture_from_the_site(mail: Callable[..., str]) -> None:
    html = mail()
    tag = _name_picture(html) or ""

    assert re.fullmatch(
        r"https://[\w.]+/static/mail/[\w-]+\.png", _found(r'src="([^"]+)"', tag) or ""
    )
    assert "media<span" not in html
    assert "data:" not in html and "<svg" not in html


@every_mail
def test_the_picture_of_the_name_is_there_at_twice_the_size_it_is_shown(
    mail: Callable[..., str],
) -> None:
    tag = _name_picture(mail()) or ""
    shown = [int(_found(rf'{side}="(\d+)"', tag) or 0) for side in ("width", "height")]
    src = _found(r'src="[^"]*/static/([^"]+)"', tag) or "missing.png"
    png = (_STATIC_DIR / src).read_bytes() if (_STATIC_DIR / src).is_file() else b""

    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert list(struct.unpack(">II", png[16:24])) == [2 * n for n in shown]
