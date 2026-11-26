"""The top bar: islands of glass over the top of the page, with no row or strip of its own."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

_WEB = Path(__file__).resolve().parents[3] / "src" / "mediaman" / "web"
_TEMPLATES = Jinja2Templates(directory=str(_WEB / "templates"))
NAV = _WEB / "static/css/_nav.css"
SEARCH = _WEB / "static/css/_watch_search.css"
PHONE = "(max-width: 699.98px)"


def _request(path: str, role: str = "admin") -> Request:
    state = {"user_role": role, "username": "robin"}
    return Request(
        {"type": "http", "path": path, "query_string": b"", "headers": [], "state": state}
    )


def _bar(template: str, path: str = "/", role: str = "admin", **ctx: object) -> str:
    html = _TEMPLATES.env.get_template(template).render(request=_request(path, role), **ctx)
    return html.split('<header class="nav-glass')[1].split("</header>")[0]


def _css(path: Path) -> str:
    return re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)


def _blocks(css: str) -> list[tuple[str, int, int]]:
    """Each top-level at-rule (`@media ...`, `@supports ...`): its prelude, start and end."""
    out = []
    for m in re.finditer(r"@(?:media|supports|keyframes)([^{]*)\{", css):
        if out and m.start() < out[-1][2]:
            continue
        depth, i = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        out.append((m.group(1), m.start(), i))
    return out


def _inside(css: str, at: str) -> str:
    """The bodies of every at-rule whose prelude holds *at*."""
    return "".join(css[css.index("{", s) + 1 : e - 1] for pre, s, e in _blocks(css) if at in pre)


def _outside(css: str) -> str:
    """What stands outside every at-rule."""
    for _, s, e in reversed(_blocks(css)):
        css = css[:s] + css[e:]
    return css


def _of(selector: str, css: str) -> str:
    return "".join(
        body
        for head, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
        if selector in [s.strip() for s in re.split(r",(?![^()]*\))", head)]
    )


def test_the_storage_and_the_profile_button_share_one_island_after_the_search() -> None:
    bar = _bar("_watch_bar.html", is_admin=True, view="rows", q="")

    end = bar.split('<div class="nav-end">')[1]
    assert "space-meter" in end and "profile-btn" in end
    assert bar.index('id="watch-search"') < bar.index('<div class="nav-end">')


def test_a_viewer_s_island_at_the_end_holds_the_profile_button_alone() -> None:
    end = _bar("_watch_bar.html", role="viewer", is_admin=False, view="rows", q="").split(
        '<div class="nav-end">'
    )[1]

    assert "profile-btn" in end and "space-meter" not in end


@pytest.mark.parametrize("path", ["/", "/watch/movie/1"])
def test_watch_is_the_current_page_at_its_root_address_and_under_it(path) -> None:
    brand = re.search(r'<a class="brand"[^>]*>', _bar("_nav.html", path)).group(0)  # type: ignore[union-attr]

    assert 'aria-current="page"' in brand


def test_another_page_does_not_mark_watch_as_current() -> None:
    assert 'aria-current="page"' not in _bar("_nav.html", "/downloads")


def test_the_bar_draws_no_row_of_its_own_and_lets_a_press_between_its_islands_through() -> None:
    css = _css(NAV)

    assert ".nav-glass::before" not in css
    own = _of(".nav-glass", _outside(css))
    assert "background" not in own
    assert "pointer-events: none" in own
    assert "pointer-events: auto" in _of(".nav-glass > *", _outside(css))


def test_no_page_has_a_strip_at_the_foot_of_a_phone() -> None:
    assert set(re.findall(r"--foot: ([^;]+);", _css(NAV))) == {"0px"}
    for template, ctx in (("_nav.html", {}), ("_watch_bar.html", {"is_admin": True, "q": ""})):
        html = _TEMPLATES.env.get_template(template).render(request=_request("/"), **ctx)
        assert "nav-foot" not in html


def test_the_islands_are_glass_and_solid_where_glass_cannot_be_drawn_or_is_not_wanted() -> None:
    css = _css(NAV)
    glass = _of(".nav-glass", _outside(css))
    assert re.search(r"--isle-blur: blur\(\d+px\) saturate\([\d.]+\)", glass)
    for island in (".nav-end", ".nav-back"):
        own = _of(island, _outside(css))
        assert "backdrop-filter: var(--isle-blur)" in own, island
        assert "background: var(--isle)" in own, island
    for at in ("(prefers-reduced-transparency: reduce)", "not ((backdrop-filter"):
        solid = _of(".nav-glass", _inside(css, at))
        assert "--isle-blur: none" in solid, at
        assert re.search(r"--isle: var\(--s\d\)", solid), at


def test_in_forced_colours_every_island_is_a_system_coloured_box() -> None:
    css = _css(NAV)
    for island in (".nav-end", ".nav-back"):
        assert "border: 1px solid var(--isle-edge)" in _of(island, _outside(css)), island
    forced = _of(".nav-glass", _inside(css, "(forced-colors: active)"))
    assert "--isle: Canvas" in forced and "--isle-edge: CanvasText" in forced


def test_the_search_field_is_the_same_glass() -> None:
    pill = _of(".wbar .watch-search::before", _outside(_css(SEARCH)))

    assert "backdrop-filter: var(--isle-blur)" in pill
    assert "background: var(--isle)" in pill


def test_the_logo_takes_its_own_glass_once_the_page_has_moved_under_it() -> None:
    css = _css(NAV)
    scrolled = _of(".nav-glass.is-scrolled .brand::before", _outside(css))

    assert "opacity: 1" in scrolled
    assert "backdrop-filter: var(--isle-blur)" in _of(".nav-glass .brand::before", _outside(css))


def test_watch_s_picture_runs_up_under_the_bar() -> None:
    hero = _of("#watch-browse > .watch-banner:first-child", _outside(_css(SEARCH)))

    assert "margin-top: calc(-1 * var(--nav-h))" in hero


def test_on_a_phone_the_search_is_a_44px_round_button_at_the_top() -> None:
    phone = _of(".wbar .watch-search", _inside(_css(SEARCH), PHONE))

    assert "width: 44px" in phone and "height: 44px" in phone
    assert "bottom" not in phone and "position: fixed" not in phone


def test_on_a_phone_the_bar_stays_at_the_top() -> None:
    phone = _of(".nav-glass", _inside(_css(NAV), PHONE))

    assert "position: relative" not in phone
    assert "--nav-h: 0px" not in _css(NAV)
