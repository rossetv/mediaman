"""The words shown while a person's picks are made, run under node against a fake DOM."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

HARNESS = Path(__file__).parent / "js" / "watch_making_harness.js"
CSS = Path(__file__).resolve().parents[3] / "src/mediaman/web/static/css/_watch_making.css"


@pytest.fixture(scope="module")
def out() -> dict[str, Any]:
    result = subprocess.run(["node", str(HARNESS)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_lines_are_many_and_each_is_there_once_and_fits_two_lines_of_a_phone(
    out: dict[str, Any],
) -> None:
    sets = out["sets"]
    assert len(sets["lines"]) >= 60
    every = [line for name in ("lines", "few", "late") for line in sets[name]]
    assert len(set(every)) == len(every)
    assert [line for line in every if len(line) > 34] == []


def test_no_line_tells_of_a_step_or_a_count_that_nobody_knows(out: dict[str, Any]) -> None:
    every = [line for name in ("lines", "few", "late") for line in out["sets"][name]]
    told = re.compile(r"\d|%|\b(almost|nearly|finishing|halfway|step|last one)\b", re.IGNORECASE)
    assert [line for line in every if told.search(line)] == []


def test_a_run_says_no_line_twice_until_all_have_been_said(out: dict[str, Any]) -> None:
    lines = out["sets"]["lines"]
    said = [line for line in out["run"] if line in lines]
    assert len(set(said[: len(lines)])) == len(lines)
    assert all(a != b for a, b in zip(out["run"], out["run"][1:], strict=False))


def test_the_second_line_says_how_long_it_can_take(out: dict[str, Any]) -> None:
    assert out["run"][0] in out["sets"]["lines"]
    assert out["run"][1] == "It can take a minute or two."


def test_two_runs_after_each_other_are_never_in_the_same_order(out: dict[str, Any]) -> None:
    first, second = out["sameChance"]
    assert first != second
    assert first[0] != second[0]


def test_a_long_wait_has_lines_of_its_own_and_a_short_one_none(out: dict[str, Any]) -> None:
    late = out["sets"]["late"]
    beats = [i for i, line in enumerate(out["run"]) if line in late]
    assert min(beats) * out["motion"][0]["ms"] >= 80_000
    heard = [out["run"][i] for i in beats]
    assert len(set(heard[: len(late)])) == len(late)


def test_someone_who_rated_few_titles_is_met_with_a_line_for_them(out: dict[str, Any]) -> None:
    few = out["sets"]["few"]
    assert out["few"][0] in few
    assert set(few) & set(out["run"]) == set()


def test_the_words_stand_still_in_a_hidden_tab(out: dict[str, Any]) -> None:
    at = out["hiddenTab"]
    assert (at["before"], at["hidden"], at["back"], at["stopped"]) == (1, 0, 1, 0)
    assert at["same"] == at["first"]
    assert at["startedHidden"] == 0


def test_reduced_motion_changes_the_words_less_often_and_fades_nothing(
    out: dict[str, Any],
) -> None:
    moving, still = out["motion"]
    assert still["ms"] >= 2 * moving["ms"]
    assert (moving["during"], moving["after"]) == (2, 1)
    assert (still["during"], still["after"]) == (1, 1)


def test_the_end_is_said_for_a_moment_and_a_stopped_one_is_not_told_twice(
    out: dict[str, Any],
) -> None:
    assert out["end"] == {
        "words": "Here they are.",
        "beats": 0,
        "ending": True,
        "early": 0,
        "over": 1,
        "cut": 1,
    }


def test_no_changing_line_reaches_a_screen_reader(out: dict[str, Any]) -> None:
    assert out["hiddenFromReaders"] == ["true", False, "true"]


def test_the_words_move_only_where_motion_is_wanted() -> None:
    css = CSS.read_text(encoding="utf-8")
    moving = re.search(r"@media \(prefers-reduced-motion: no-preference\) \{\n(.*?)\n\}", css, re.S)
    assert moving
    rest = css.replace(moving[0], "")
    assert "animation:" in moving[1]
    assert re.findall(r"animation: (?!loading-pulse)[^;]+", rest) == []
