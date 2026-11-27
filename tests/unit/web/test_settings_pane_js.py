"""Settings as a pane over Watch: its sections, its unsaved edits and the way out of it.

The real scripts load after the fragment is in the page, as the pane loads them, and run
under node against a small fake DOM with the shell's API stubbed.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "settings_pane_harness.js"


def _run(scenario: str):
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_an_edit_stays_unsaved_across_sections_and_only_leaving_settings_asks() -> None:
    out = _run("acrossSections")
    assert out["gone"] == ["settings/integrations"]
    assert out["on"] == "integrations"
    assert out["unsaved"] is True
    assert out["status"] == "In General & Schedule"
    assert out["leaving"] == {"toSection": False, "toHistory": True, "closed": True}


def test_typing_in_watch_search_does_not_make_settings_unsaved() -> None:
    out = _run("watchSearch")
    assert out == {
        "unsaved": False,
        "bar": False,
        "leaving": {"toSection": False, "toHistory": False, "closed": False},
    }


def test_an_integrations_address_opens_and_focuses_its_card() -> None:
    out = _run("openaiAddress")
    assert out["on"] == "integrations"
    assert out["open"] is True
    assert out["focused"] is True
    assert out["revealed"] == ["intg-openai"]


def test_the_old_link_to_the_openai_card_steps_to_it_and_opens_it() -> None:
    out = _run("openaiLink")
    assert out["prevented"] is True
    assert out["revealed"] == ["intg-openai"]
    assert out["gone"] == ["settings/integrations/openai"]
    assert (out["on"], out["open"], out["focused"]) == ("integrations", True, True)


def test_up_from_a_section_shows_the_list_again_with_its_row_focused_or_overview_beside_it() -> (
    None
):
    out = _run("upToList")
    assert out["narrow"] == {"inside": True, "after": False, "on": None, "focused": "integrations"}
    assert out["wide"] == {"inside": False, "after": False, "on": "overview", "focused": None}


def test_an_expired_session_keeps_unsaved_edits_and_otherwise_signs_in_back_to_the_pane() -> None:
    out = _run("expired")
    assert out["edited"]["href"] == "/?you=settings/general"
    assert any("expired" in m for m in out["edited"]["said"])
    assert out["clean"]["href"] == "/login?next=%2F%3Fyou%3Dsettings%2Fgeneral"


def test_a_window_widened_with_a_section_open_brings_the_section_list_back_beside_it() -> None:
    out = _run("widened")
    assert out["narrow"] == {
        "inside": True,
        "listInert": True,
        "on": "integrations",
        "current": "integrations",
    }
    # the list is in reach again with no step taken, its row for the open section marked
    assert out["wide"] == {
        "inside": False,
        "listInert": False,
        "on": "integrations",
        "current": "integrations",
    }
    assert out["gone"] == []


def test_the_save_bar_alone_marks_unsaved_edits_and_the_pane_s_discard_takes_it_down() -> None:
    out = _run("barMarked")
    assert out["before"] == {"bar": False, "page": ""}
    # the bar shows itself, and the page under the pane is not marked
    assert out["dirty"] == {"bar": True, "page": ""}
    assert out["discarded"] == {"bar": False, "page": ""}


def test_the_page_is_told_only_while_the_save_bar_is_on_show() -> None:
    out = _run("barLifts")
    assert out == {"before": False, "dirty": True, "away": False, "back": True, "discarded": False}
