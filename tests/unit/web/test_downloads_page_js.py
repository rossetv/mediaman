"""The /downloads page scripts keep the page in step with each poll.

The real scripts run under node against a small fake DOM, so these check what
the page ends up showing, not how the scripts are written.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "downloads_harness.js"


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_page_follows_the_poll_payload() -> None:
    out = _run("page")
    # hero and queue come back after the page went empty; a new item gets a row
    assert out["afterRefill"] == {"hero": 1, "rows": ["q2"]}
    assert out["withNew"] == ["q2", "q3"]
    # the abandon button appears with the payload, carries stuck seasons, and goes again
    assert out["shown"]["hero"] == 1
    assert out["shown"]["row"] == 1
    assert json.loads(out["shown"]["stuck"]) == [{"number": 2, "missing_episodes": 3}]
    assert out["hidden"] == {"hero": 0, "row": 0}
    # a departed row is removed even when no animation event arrives
    assert out["afterExit"] == ["q3"]


def test_poll_now_during_a_poll_runs_another_straight_after() -> None:
    assert _run("poll")["calls"] == 2


def test_recent_grid_is_rebuilt_only_when_it_changes() -> None:
    out = _run("recent")
    assert out["kept"] is True
    assert out["count"] == 3


def test_confirm_says_it_is_working_and_recovers_on_failure() -> None:
    out = _run("abandon")
    assert out["idle"] == "Abandon"
    assert out["working"] == "Abandoning…"
    assert out["disabled"] == "true"
    assert out["after"] == "Abandon"
    assert out["reenabled"] == "false"
