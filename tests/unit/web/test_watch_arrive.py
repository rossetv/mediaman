"""Watch straight after signing in, and on log-out: the page's half of the move from and to the
sign-in page, which the two pages meet on as one frame of black and the sign-in page's lights."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import jinja2
import pytest

_WEB = Path(__file__).resolve().parents[3] / "src/mediaman/web"
HARNESS = Path(__file__).parent / "js" / "watch_arrive_harness.js"
CSS = _WEB / "static/css"
NOW = 5000000

_ENV = jinja2.Environment(
    loader=jinja2.ChoiceLoader(
        [
            jinja2.DictLoader({"_nav.html": "{% block nav_center %}{% endblock %}"}),
            jinja2.FileSystemLoader(_WEB / "templates"),
        ]
    ),
    autoescape=True,
)


def _arrive(stored: str | None) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), json.dumps(stored)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _watch(banner: dict | None = None) -> str:
    ctx = {"banner": banner, "rows": [], "watch_error": None, "is_admin": False, "page_data": {}}
    return _ENV.get_template("watch.html").render(**ctx)


def test_watch_opened_straight_after_signing_in_arrives_from_where_the_lights_were() -> None:
    sky = ["matrix(1.02, 0, 0, 1.02, 12, 8)", "matrix(1.01, 0, 0, 1.01, -9, -5)"]
    out = _arrive(json.dumps({"t": NOW - 900, "sky": sky}))
    assert out["painted"] == ["is-arriving"]
    assert out["props"] == {"--sky-0": sky[0], "--sky-1": sky[1]}
    # the note is used once: a reload is a normal load
    assert out["left"] == []
    # and once played, what the page draws later does not come in with it
    assert out["playedMs"] == [1200]
    assert out["after"] == []


@pytest.mark.parametrize(
    "stored",
    [
        None,
        json.dumps({"t": NOW - 30001, "sky": []}),
        json.dumps({"t": NOW + 60000, "sky": []}),
        "{not json",
        "null",
    ],
    ids=["normal-load", "stale", "from-the-future", "broken", "empty"],
)
def test_any_other_load_of_watch_plays_nothing_and_keeps_no_note(stored: str | None) -> None:
    out = _arrive(stored)
    assert (out["painted"], out["props"], out["playedMs"]) == ([], {}, [])
    assert out["left"] == []


def test_the_note_is_read_in_the_head_before_the_first_paint() -> None:
    html = _watch()
    head = html.split("</head>")[0]
    tag = re.search(r'<script src="/static/js/watch/arrive\.js\?v=[^"]*"[^>]*>', head)
    assert tag, "arrive.js is not in the head"
    assert "defer" not in tag[0] and "async" not in tag[0]


def test_the_lights_are_drawn_outside_the_page_that_fades() -> None:
    html = _watch()
    sky = html.index('<div class="mm-sky" aria-hidden="true"><i></i><i></i></div>')
    assert sky > html.index("</main>")


def test_the_banner_has_its_picture_blurred_beside_it_and_none_without_one() -> None:
    play = {"href": "/watch/dune", "label": "Play"}
    art = {
        "title": "Dune",
        "backdrop_url": "/art/dune.jpg",
        "next_backdrop_url": "/art/dune-2.jpg",
        "poster_url": "/p/dune.jpg",
        "primary": play,
    }
    html = _watch(art)
    # it falls back the way the banner's picture does, and is marked when none loads
    assert re.search(
        r'data-wide-art data-broken-on-error>\s*(\{#.*?#\}\s*)?<img class="mm-bg-blur" src="/art/dune.jpg" alt=""'
        r' data-next-src="/art/dune-2.jpg" data-broken-on-error>',
        html,
    )
    assert "mm-bg-blur" not in _watch(
        {"title": "Dune", "poster_url": "/p/dune.jpg", "primary": play}
    )


def _rules(css: str) -> list[str]:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [sel.strip() for sel in re.findall(r"([^{}]+)\{[^{}]*animation[^{}]*\}", css)]


# Where each file's signing-in-and-out part starts.
_PARTS = {
    "_watch.css": "/* Signing in and out.",
    "_nav.css": "/* Signing in and out",
    "_you.css": "/* Log-out (nav.js)",
    "_ui_feedback.css": "/* A yes that leaves the page",
}


def test_a_normal_load_plays_nothing_every_motion_waits_for_signing_in_or_out() -> None:
    for name, start in _PARTS.items():
        css = (CSS / name).read_text()
        rules = _rules(css[css.index(start) :])
        assert rules, name
        for selector in rules:
            if "{" in selector or selector.startswith(("from", "to", "@", "0%")):
                continue
            for one in re.split(r",(?![^()]*\))", selector):
                one = one.strip()
                # data-leave: put on a confirm only as its yes leaves the page (ui-feedback.js)
                assert one.startswith(
                    (
                        "html.is-arriving",
                        "html.is-leaving",
                        "html.has-watch-search",
                        ".ui-confirm-overlay[data-leave]",
                    )
                ), (name, one)


def _frames(name: str) -> str:
    css = "".join((CSS / f).read_text() for f in ("_nav.css", "_watch.css"))
    return re.search(r"@keyframes " + name + r" \{([^\n]*)\}\n", css)[1]


def _calm_fades() -> dict[str, str]:
    base = re.sub(r"/\*.*?\*/", "", (CSS / "_base.css").read_text(), flags=re.S)
    calm = re.findall(r"@media \(prefers-reduced-motion: reduce\) \{\n(.*?)\n\}", base, flags=re.S)
    fades = [block for block in calm if "is-arriving" in block]
    assert len(fades) == 1
    return {
        sel.strip(): body.strip() for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", fades[0])
    }


def test_under_reduced_motion_the_move_is_a_150ms_fade_and_nothing_moves() -> None:
    rules = _calm_fades()
    assert set(rules) == {
        "html.is-arriving :is(#main-content, .nav-glass > :not(.brand, .watch-search),"
        " .nav-glass :is(.brand, .watch-search) > *)",
        "html.is-arriving .nav-glass :is(.brand, .watch-search)::before",
        "html.is-arriving .mm-sky",
        "html.is-leaving body::after",
    }
    for body in rules.values():
        assert "var(--dur-fast) linear" in body and body.endswith("!important;")
        frames = _frames(re.search(r"animation: ([\w-]+)", body)[1])
        assert "opacity" in frames and "transform" not in frames


# Just enough of a selector matcher for the arrival rules: tags, ids, classes, `*`, :is(), :not(),
# a pseudo-element at the end, and the descendant and child combinators.
def _top(text: str, seps: str) -> list[tuple[str, str]]:
    """*text* cut at each of *seps* outside brackets, as (separator before, piece) pairs."""
    out, depth, start, sep = [], 0, 0, ""
    for i, ch in enumerate(text):
        depth += (ch == "(") - (ch == ")")
        if depth == 0 and ch in seps:
            out.append((sep, text[start:i]))
            start, sep = i + 1, ch
    out.append((sep, text[start:]))
    return [(s, p.strip()) for s, p in out if p.strip()]


def _compound(comp: str, node: tuple[str, str, set[str]], chain: list) -> bool:
    tag, ident, classes = node
    for kind, name, args in re.findall(r"(::?|[.#]|)([\w*-]+)(\((?:[^()]|\([^()]*\))*\))?", comp):
        if kind in ("", "*") and name not in ("*", tag):
            return False
        if (kind == "." and name not in classes) or (kind == "#" and name != ident):
            return False
        if kind == ":":
            hit = any(_complex(arg, chain) for _, arg in _top(args[1:-1], ","))
            if hit != (name == "is"):
                return False
    return True


def _complex(sel: str, chain: list, pseudo: str = "") -> bool:
    """Whether *sel* matches the last element of *chain* (and its *pseudo*-element, if any)."""
    parts = _top(re.sub(r"\s*>\s*", ">", sel), " >")
    *rest, (_, last) = parts
    last, _, own = last.partition("::")
    if own != pseudo:
        return False  # a pseudo-element inside :is() or :not() is dropped, so it matches nothing

    def walk(i: int, j: int) -> bool:
        if not _compound(parts[i][1] if i < len(parts) - 1 else last, chain[j], chain[: j + 1]):
            return False
        if i == 0:
            return True
        up = range(j - 1, -1, -1) if parts[i][0] == " " else range(j - 1, j - 2, -1)
        return any(walk(i - 1, k) for k in up if k >= 0)

    return walk(len(parts) - 1, len(chain) - 1)


_PAGE = [("html", "", {"is-arriving"}), ("body", "", set())]
_BAR = [*_PAGE, ("header", "", {"nav-glass"})]
_BRAND = [*_BAR, ("a", "", {"brand"})]
_SEARCH = [*_BAR, ("div", "", {"watch-search"})]


def _faded(chain: list, pseudo: str = "") -> bool:
    return any(
        _complex(one, chain, pseudo)
        for sel, body in _calm_fades().items()
        if "mm-fade-in" in body
        for _, one in _top(sel, ",")
    )


def test_under_reduced_motion_signing_in_fades_each_island_and_its_glass() -> None:
    for chain, pseudo in [
        ([*_PAGE, ("main", "main-content", set())], ""),
        ([*_BAR, ("a", "", {"nav-back"})], ""),
        ([*_BAR, ("div", "", {"nav-end"})], ""),
        (_BRAND, "before"),
        ([*_BRAND, ("svg", "", {"ico", "wordmark"})], ""),
        (_SEARCH, "before"),
        ([*_SEARCH, ("div", "", {"watch-search__inner"})], ""),
    ]:
        assert _faded(chain, pseudo), (chain[-1], pseudo)


def test_under_reduced_motion_signing_in_fades_no_element_that_holds_glass() -> None:
    # Glass under a fading parent sees none of the page while it fades, then snaps to its blur.
    for chain in (_PAGE, _BAR, _BRAND, _SEARCH):
        assert not _faded(chain), chain[-1]


def test_under_reduced_motion_log_out_covers_the_page_in_its_own_black_instead_of_fading_it() -> (
    None
):
    rules = _calm_fades()
    assert not any(sel.startswith("html.is-leaving body") and "::" not in sel for sel in rules)
    cover = rules["html.is-leaving body::after"]
    # Over everything, in the colour the page went out to when it faded.
    assert "position: fixed" in cover and "inset: 0" in cover and "z-index: 2147483647" in cover
    assert "background: var(--bg)" in cover and "animation: mm-in " in cover
