"""A row that arrives mid-list on the /downloads page, run through the page's real scripts."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "downloads_harness.js"


def test_a_new_row_mid_list_opens_from_nothing_and_the_row_below_never_jumps() -> None:
    result = subprocess.run(
        ["node", str(HARNESS), "newRowMidList"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    rows = {dl_id: (slide, top) for dl_id, _, slide, top in json.loads(result.stdout)}
    # the row below stays where it was seen, and is pushed down only as the new row grows
    assert rows["q3"] == ("", 100)
