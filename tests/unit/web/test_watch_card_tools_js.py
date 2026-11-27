"""watch/card-tools.js run under node against a fake DOM: Escape and a card's open levels, and the
poster's words under a level's name."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "card_tools_escape_harness.js"
COVER = Path(__file__).parent / "js" / "card_tools_cover_harness.js"


def test_escape_shuts_the_levels_unless_a_layer_over_them_took_it() -> None:
    result = subprocess.run(["node", str(HARNESS)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["first"] == {"opened": True, "open": False, "taken": True}
    assert out["takenBefore"] == {"opened": True, "open": True, "taken": True}


def test_a_poster_s_words_give_way_only_to_the_levels_and_a_level_s_name_that_lie_over_them() -> (
    None
):
    result = subprocess.run(["node", str(COVER)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["opened"]
    scores, both = ["watch-card__scores"], ["watch-card__scores", "watch-card__title"]
    # The scores reach under the column of levels, from the moment it rises until it is shut.
    assert out["lifted"] == out["off"] == scores
    # Best first: the top two names lie over nothing, the badge at the top included.
    assert out["at"] == [scores, scores, both, both]
    # A name the page does not draw, too narrow for its words or too small a poster, covers nothing.
    assert out["invisible"] == out["undrawn"] == scores
    # A title wrapped anew by a resize is measured again.
    assert out["resized"] == scores
    # The words that say a level was taken back are wider than its name, and measured as they are.
    assert out["given"] == scores
    assert out["removedSays"] == "Rating removed" and out["removed"] == both
    assert out["shut"] == []
    # Beside the name, a line is covered only where its words reach under it.
    assert out["lines"] == ["line-1"]
