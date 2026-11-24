"""The rows' arrows, run under node against a fake DOM with a layout."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "watch_rows_harness.js"


def test_paging_a_line_of_seasons_there_and_back_leaves_none_out() -> None:
    result = subprocess.run(
        ["node", str(HARNESS), "seasons"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    for way in ("there", "back"):
        stops = out["phone"][way]
        assert sorted({n for stop in stops for n in stop}) == list(range(1, 11)), way
        # A pill wider than the line is never whole, and is no trap.
        assert {n for stop in out["wide"][way] for n in stop} == {1, 2, 3, 5, 6}, way
    assert out["phone"]["back"][-1][0] == 1
