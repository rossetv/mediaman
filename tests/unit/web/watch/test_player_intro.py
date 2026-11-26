"""The name drawn over the player while a title starts, and handed over to the picture."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from .test_player_js import EPISODE, REPO, _episode, _markup, run

CSS = REPO / "src/mediaman/web/static/css/_player.css"


@pytest.fixture(scope="module")
def markup(tmp_path_factory) -> Path:
    return _markup(tmp_path_factory, EPISODE)


def test_the_name_shows_once_a_start_has_taken_a_third_of_a_second_and_goes_as_it_plays(markup):
    out = run(markup, "intro_slow")
    assert out["early"] == {"intro": False, "starting": True, "loading": True}
    assert out["late"]["intro"] is True
    assert out["waits"]["intro"] is True
    assert out["playing"] == {"intro": False, "starting": False, "loading": False}


def test_a_title_that_starts_at_once_never_shows_the_name(markup):
    assert run(markup, "intro_fast")["ever"] is False


def test_buffering_and_seeking_while_it_plays_keep_the_spinner(markup):
    out = run(markup, "intro_midplay")
    for state in (out["waiting"], out["seeking"]):
        assert state == {"intro": False, "starting": False, "loading": True}


def test_the_next_episode_starts_under_the_name_and_lets_its_last_frame_go_as_it_plays(
    markup, tmp_path_factory
):
    pages = {"/watch/play/502": _markup(tmp_path_factory, _episode(2))}
    out = run(markup, "intro_next", pages=pages)
    assert out["loading"] == {"intro": True, "starting": True, "loading": True, "held": True}
    assert out["playing"] == {"intro": False, "starting": False, "loading": False, "held": False}


def test_a_title_sent_to_a_tv_as_it_starts_shows_no_name(markup):
    assert run(markup, "away_intro")["intro"] is False


def _split(css: str, query: str) -> tuple[list[str], str]:
    """The bodies of the `@media (prefers-reduced-motion: <query>)` blocks, and the rest."""
    blocks, rest, at = [], [], 0
    for m in re.finditer(rf"@media\s*\(prefers-reduced-motion:\s*{query}\)\s*\{{", css):
        if m.start() < at:
            continue
        rest.append(css[at : m.start()])
        depth, i = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        blocks.append(css[m.end() : i - 1])
        at = i
    return blocks, "".join(rest) + css[at:]


def test_under_reduced_motion_the_name_only_fades():
    css = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)
    moving, still = _split(css, "no-preference")
    assert any(
        re.search(r"\.player\.is-intro[^{]*\.wm-media[^{]*\{[^}]*wm-draw", b) for b in moving
    )
    assert not re.search(r"animation[^;]*(wm-draw|wm-dot|player-intro-glow)", still)
    # The base stylesheet cuts every transition short under reduced motion: the fade is kept.
    reduce, _ = _split(css, "reduce")
    fade = r"\.player-intro\s*\{[^}]*transition-duration:[^;]*!important"
    assert any(re.search(fade, b) for b in reduce)
