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
        "poster_url": "/p/dune.jpg",
        "primary": play,
    }
    html = _watch(art)
    assert re.search(
        r'data-wide-art data-broken-on-error>\s*(\{#.*?#\}\s*)?<img class="mm-bg-blur" src="/art/dune.jpg" alt="">',
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
    "_ui_feedback.css": "/* Log-out (nav.js)",
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
                assert one.startswith(
                    ("html.is-arriving", "html.is-leaving", "html.has-watch-search")
                ), (name, one)


def _frames(name: str) -> str:
    css = "".join((CSS / f).read_text() for f in ("_nav.css", "_watch.css"))
    return re.search(r"@keyframes " + name + r" \{([^\n]*)\}\n", css)[1]


def test_under_reduced_motion_the_move_is_a_150ms_fade_and_nothing_moves() -> None:
    base = (CSS / "_base.css").read_text()
    calm = re.findall(r"@media \(prefers-reduced-motion: reduce\) \{\n(.*?)\n\}", base, flags=re.S)
    fades = [block for block in calm if "is-arriving" in block]
    assert len(fades) == 1
    rules = {
        sel.strip(): body.strip() for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", fades[0])
    }
    assert set(rules) == {
        "html.is-arriving :is(#main-content, header.nav-glass)",
        "html.is-arriving .mm-sky",
        "html.is-leaving body",
    }
    for body in rules.values():
        assert "var(--dur-fast) linear" in body and body.endswith("!important;")
        frames = _frames(re.search(r"animation: ([\w-]+)", body)[1])
        assert "opacity" in frames and "transform" not in frames
