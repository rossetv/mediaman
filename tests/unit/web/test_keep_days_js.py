"""The keep page says its days as they fall where the reader is."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "js" / "keep_days_harness.js"


@pytest.mark.parametrize(
    ("zone", "shown"),
    [
        ("Pacific/Auckland", ["25 November", "in 2 days", "25 Nov", "Nov", "25"]),
        ("America/Los_Angeles", ["24 November", "in 2 days", "24 Nov", "Nov", "24"]),
        ("UTC", ["24 November", "in 2 days", "24 Nov", "Nov", "24"]),
    ],
)
def test_the_day_and_its_count_are_those_of_the_reader(zone: str, shown: list[str]) -> None:
    result = subprocess.run(
        ["node", str(HARNESS), "2026-11-24T20:00:00+00:00", "2026-11-22T23:30:00+00:00"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "TZ": zone},
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == shown


def test_a_day_of_another_year_says_which() -> None:
    result = subprocess.run(
        ["node", str(HARNESS), "2027-02-23T12:00:00+00:00", "2026-11-25T12:00:00+00:00"],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "TZ": "UTC"},
    )
    assert json.loads(result.stdout)[:3] == ["23 February 2027", "in 90 days", "23 Feb"]


def test_a_day_that_cannot_be_read_stays_as_sent() -> None:
    result = subprocess.run(
        ["node", str(HARNESS), "", "2026-11-22T23:30:00+00:00"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == ["as sent"] * 5
