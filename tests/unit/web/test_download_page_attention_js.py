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
