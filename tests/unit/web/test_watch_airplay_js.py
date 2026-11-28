"""AirPlay from the player, run under node against a stand-in for Safari's video element: where
the TV starts the stream."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.helpers.node_batch import run_node

HARNESS = Path(__file__).parent / "js" / "watch_airplay_harness.js"


def _run(scenario: str) -> Any:
    result = run_node(HARNESS, scenario)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_a_tv_that_begins_from_the_start_is_sent_to_the_last_place_once() -> None:
    out = _run("resumes")
    assert out["sent"] == [1268]
    # Seen playing there, a later move to the start is the person's own.
    assert out["later"] == []


def test_a_stream_from_its_start_is_not_moved() -> None:
    assert _run("fromStart") == {"seeks": []}
