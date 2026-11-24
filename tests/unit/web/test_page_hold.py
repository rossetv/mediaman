"""The page held still behind a dialog: the scrollbar's width while dialogs open and close over
one another, run under node against a fake DOM, and the one rule that stops the scrolling."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "page_hold_harness.js"
CSS_DIR = Path(__file__).resolve().parents[3] / "src" / "mediaman" / "web" / "static" / "css"


def _run(scenario: str):
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False, timeout=20
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_scrollbar_s_width_is_held_until_the_last_of_two_dialogs_has_closed() -> None:
    # held with both open and with one left, whichever closes first, then given back
    assert _run("stacked") == {
        "oldestFirst": ["15px", "15px", None],
        "newestFirst": ["15px", "15px", None],
    }


def test_a_save_that_finishes_under_an_open_confirm_leaves_the_width_to_the_confirm() -> None:
    # with both open, once the save's dialog has gone, once the confirm has; then a dialog
    # closed a second time takes nothing from the next one
    assert _run("saveUnderAConfirm") == ["15px", "15px", None, "15px", None]


def _rules(marker: str) -> list[tuple[str, str]]:
    """Every rule of the style sheets whose selector or declarations hold *marker*."""
    return [
        (" ".join(head.split()), body)
        for path in sorted(CSS_DIR.glob("*.css"))
        for head, body in re.findall(
            r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", path.read_text(), flags=re.S)
        )
        if marker in head or marker in body
    ]


def test_a_page_that_had_a_scrollbar_keeps_its_room_so_what_is_fixed_stays_too() -> None:
    marked = dict(_rules("page-bar"))
    assert "scrollbar-gutter: stable" in marked["html.page-bar"]
    # where the room cannot be kept, the page keeps its width, and only there, or both would act
    assert set(marked) == {"html.page-bar", "html.page-bar body"}
    assert "padding-right: var(--page-bar" in marked["html.page-bar body"]
    css = (CSS_DIR / "_ui_feedback.css").read_text()
    assert "html.page-bar body" in css.split("@supports not (scrollbar-gutter: stable) {")[1]


def test_one_rule_holds_the_page_for_every_dialog_and_leaves_a_held_html_alone() -> None:
    rules = [rule for rule in _rules("body:has(") if "overflow: hidden" in rule[1]]
    assert len(rules) == 1
    head, _ = rules[0]
    # a title's page and the storage panel hold the html: a held body there takes the nav bar away
    assert head.startswith("html:not(.has-space, .has-ttl) body:has(")
    for dialog in (
        "> .ui-confirm-overlay:not(.is-leaving)",
        '> .reauth-backdrop:not([aria-hidden="true"])',
        "> .keep-dialog-overlay.active:not(.is-closing)",
        '#abandon-modal[aria-hidden="false"]',
    ):
        assert dialog in head
