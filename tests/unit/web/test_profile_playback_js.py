"""profile-playback.js under node against a fake DOM: the Profile pane's audio language and subtitles."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import jinja2

WEB = Path(__file__).parents[3] / "src" / "mediaman" / "web"
HARNESS = Path(__file__).parent / "js" / "profile_playback_harness.js"


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False, timeout=60
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_hub_says_what_is_set_and_each_view_checks_it() -> None:
    out = _run("hub")
    assert (out["audio"], out["subs"], out["folded"]) == ("Deutsch", "Always · Français", True)
    assert out["checked"] == [["de"], ["always"], ["fr"]]
    # As the title comes, then the one set, English and the browser's languages, each once.
    assert out["suggested"] == ["", "de", "en", "ja"]
    assert out["plain"] == {
        "audio": "As the title comes",
        "subs": "When needed",
        "folded": False,
        "checked": [""],
    }


def test_the_languages_of_the_library_come_first_by_the_servers_own_names() -> None:
    out = _run("library")
    assert out["head"] == "In your library"
    # As the title comes, the one set, then the library's: not the browser's.
    assert out["first"] == ["", "en", "zh", "fr"]
    assert out["zh"].startswith("中文 (server)")


def test_a_language_picked_is_saved_alone_and_named_on_the_hub() -> None:
    out = _run("pickAudio")
    assert out["at"]["checked"] == ["fr"]
    assert out["puts"] == [["/api/account/playback", {"audio_lang": "fr"}]]
    assert out["audio"] == "Français"
    assert out["toasts"] == ["Saved. It applies from the next thing you play."]


def test_a_pick_that_was_not_saved_goes_back_and_says_so() -> None:
    out = _run("pickFails")
    assert (out["audio"], out["subs"], out["folded"]) == (["en"], ["needed"], False)
    assert out["value"] == "English"
    assert out["toasts"] == ["error: No luck", "error: No luck"]


def test_the_search_finds_a_language_once_by_either_name_and_says_when_none() -> None:
    out = _run("search")
    assert out["germ"] == {"shown": ["de"], "heads": 0}
    assert out["english"] == ["en"]
    assert out["nothing"] == {"shown": [], "none": "No language called “Klingon”."}
    assert out["cleared"] == {"heads": 2, "none": True, "count": 8}


def test_always_folds_out_its_language_in_the_one_heard_and_off_folds_it_away() -> None:
    out = _run("always")
    assert out["before"] is False
    assert out["always"] == {"folded": True, "subs": "Always · Deutsch"}
    assert out["lang"] == {"subs": "Always · 日本語", "sub": "日本語"}
    assert out["after"] == {"folded": False, "subs": "Off"}
    assert out["puts"] == [
        {"subtitle_lang": "de"},
        {"subtitle_lang": "ja"},
        {"subtitle_lang": "off"},
    ]


def test_the_watching_group_sits_between_connected_and_ai_for_every_role() -> None:
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(WEB / "templates"), autoescape=True)
    for role in ("admin", "viewer"):
        html = env.get_template("panes/_profile.html").render(
            role=role,
            username="amy",
            email=None,
            picture={"url": "/a.svg", "choice": None, "default": 1, "drawn": [], "photo": None},
            static_v="1",
        )
        assert html.index(">Connected<") < html.index(">Watching<") < html.index('id="account-ai"')
        assert 'id="account-row-audio"' in html and 'id="account-row-subs"' in html
        for view in ("profile/audio", "profile/subtitles", "profile/subtitles/language"):
            assert f'data-view="{view}"' in html
        assert "/static/js/profile-playback.js" in html
