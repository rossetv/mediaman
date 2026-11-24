"""Where the form that adds a user says why it refused, run under node against a fake DOM."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "js" / "users_add_harness.js"


@pytest.fixture(scope="module")
def refusals() -> dict:
    result = subprocess.run(["node", str(HARNESS)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    ("refusal", "field", "words"),
    [
        ("shortName", "new-username", "3 characters"),
        ("takenName", "new-username", "No"),
        ("shortPassword", "new-user-password", "12 characters"),
        ("weakPassword", "new-user-password", "NoAdd a digit"),
    ],
)
def test_a_refused_field_has_the_cursor_and_its_reason_beside_it(
    refusals, refusal, field, words
) -> None:
    out = refusals[refusal]
    assert out["said"] == [field + "-why"]
    assert words in out["words"]
    assert out["focused"] == field
    assert out["last"] == [field, field + "-why"]


@pytest.mark.parametrize("refusal", ["limit", "offline", "serverError"])
def test_a_refusal_no_field_can_put_right_is_said_at_the_button_pressed(refusals, refusal) -> None:
    out = refusals[refusal]
    assert out["said"] == ["create-user-result"]
    assert out["focused"] == "btn-submit-add-user"
    assert out["last"][-1] == "create-user-result"


def test_such_a_refusal_leaves_a_person_who_moved_on_where_they_are(refusals) -> None:
    assert refusals["limitElsewhere"]["focused"] == "elsewhere"


def test_a_reason_goes_once_the_person_types(refusals) -> None:
    assert [out["typed"] for out in refusals.values()] == [[]] * len(refusals)
