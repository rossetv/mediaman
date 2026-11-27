"""watch/card-tools.js run under node against a fake DOM: Escape and a card's open levels."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "card_tools_escape_harness.js"


def test_escape_shuts_the_levels_unless_a_layer_over_them_took_it() -> None:
    result = subprocess.run(["node", str(HARNESS)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["first"] == {"opened": True, "open": False, "taken": True}
    assert out["takenBefore"] == {"opened": True, "open": True, "taken": True}
