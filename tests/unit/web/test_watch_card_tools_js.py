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


def test_a_poster_s_words_give_way_only_to_a_level_s_name_at_their_height_and_only_while_it_shows() -> (
    None
):
    result = subprocess.run(["node", str(COVER)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["opened"]
    # Best first: the top two names lie over nothing, the badge at the top included.
    assert [sorted(c) for c in out["at"]] == [
        [],
        [],
        ["watch-card__title"],
        ["watch-card__scores", "watch-card__title"],
    ]
    assert out["off"] == out["shut"] == []
    # Beside the name, a line is covered only where its words reach under it.
    assert out["lines"] == ["line-1"]
