"""The page a download was asked for on, of one Radarr or Sonarr will not import without a
person, run under node against a fake DOM."""

from __future__ import annotations

from tests.unit.web.test_download_page_js import _run


def test_it_says_it_needs_attention_with_no_bar_and_no_colour_of_almost_ready() -> None:
    seen = _run("attention")
    almost = _run("run")["almost"]
    assert (seen["step"], seen["bar"], seen["said"]) == (
        "Needs attention",
        False,
        "Needs attention",
    )
    assert seen["colour"] < almost["colour"]


def test_a_download_being_found_another_copy_of_is_not_called_searching() -> None:
    seen = _run("finding")
    assert (seen["bar"], seen["said"]) == (False, "Finding another copy")
    assert "Finding another copy" in seen["words"]
    assert "Searching" not in seen["words"]
