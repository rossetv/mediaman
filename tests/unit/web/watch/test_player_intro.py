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


def test_a_start_that_fails_under_the_name_shows_its_message_clear_of_it(markup):
    out = run(markup, "intro_error")
    assert out["before"]["intro"] is True
    assert out["failed"] == {
        "intro": False,
        "starting": False,
        "loading": False,
        "out": False,
        "message": True,
    }


def test_try_again_shows_the_name_again_and_it_goes_as_the_title_plays(markup):
    out = run(markup, "intro_error")
    assert out["again"] == {"intro": True, "starting": True, "loading": True, "message": False}
    assert out["playing"] == {"intro": False, "starting": False, "loading": False}


def test_seeking_before_a_title_has_first_played_shows_no_name(markup):
    out = run(markup, "intro_seek_first")
    assert out["intro"] is False
    assert out["starting"] is False


def test_back_from_the_page_cache_leaves_the_name_to_the_new_start(markup):
    out = run(markup, "intro_back")
    assert out["shown"]["intro"] is True
    assert out["after"] == {"intro": False, "starting": False, "loading": False}


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
        re.search(r"\.player(?:\.is-intro|:is\(\.is-intro)[^{]*\.wm-media[^{]*\{[^}]*wm-draw", b)
        for b in moving
    )
    assert not re.search(r"animation[^;]*(wm-draw|wm-dot|player-intro-glow)", still)
    # The base stylesheet cuts every transition short under reduced motion: the fade is kept.
    reduce, _ = _split(css, "reduce")
    fade = r"\.player-intro\s*\{[^}]*transition-duration:[^;]*!important"
    assert any(re.search(fade, b) for b in reduce)


def test_the_name_fades_from_where_its_drawing_stopped_and_the_spinner_is_hidden_by_opacity():
    css = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)
    assert re.search(
        r"\.player:is\(\.is-intro, \.is-intro-out\)[^{]*wm-man > path\s*\{[^}]*wm-draw", css
    )
    assert re.search(r"\.is-intro-out[^{]*\{[^}]*animation-play-state:\s*paused", css)
    assert re.search(r"\.is-starting \.player-spinner\s*\{\s*opacity:\s*0", css)


def test_under_reduced_motion_the_pulsing_spinner_stays_hidden_under_the_name():
    css = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)
    reduce, _ = _split(css, "reduce")
    hide = r"\.is-starting\.is-loading \.player-spinner\s*\{[^}]*animation:\s*none\s*!important"
    assert any(re.search(hide, b) for b in reduce)


def test_a_second_name_starts_its_drawing_over_once_the_first_has_gone_out(
    markup, tmp_path_factory
):
    pages = {"/watch/play/502": _markup(tmp_path_factory, _episode(2))}
    out = run(markup, "intro_restart", pages=pages)
    assert out["out"] is True
    assert out["intro"] is True
    assert [False, False] in out["reflows"]
