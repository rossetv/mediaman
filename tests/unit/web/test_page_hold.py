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
    rules = [rule for rule in _rules("page-held") if "overflow: hidden" in rule[1]]
    assert len(rules) == 1
    head, _ = rules[0]
    # a title's page, the storage panel and the TV remote hold the html: a held body there
    # takes the nav bar away
    assert head == "html.page-held:not(.has-space, .has-ttl, .has-tv-remote) body"


def test_the_page_is_marked_held_from_the_first_dialog_to_the_last() -> None:
    assert _run("heldMark") == [False, True, True, False]


def test_the_password_dialog_marks_the_page_held_while_it_is_open() -> None:
    assert _run("reauthHeldMark") == [False, True, False]


JS_DIR = CSS_DIR.parent / "js"


def test_the_keep_dialog_the_picture_dialog_and_the_abandon_dialog_hold_the_page_as_they_open() -> (
    None
):
    # no harness runs these three, so the code path of each is read: the hold is taken
    # just before the dialog opens, and given back as it closes
    paths = {
        "watch/admin.js": r"modal\.held = MM\.dom\.holdPage\(\);\s*modal\.open\(\)",
        "profile-picture.js": r"letGo = MM\.dom\.holdPage\(\);[\s\S]{0,200}?modal\.open\(\)",
        "dl-abandon.js": r"letGo = MM\.dom\.holdPage\(\);\s*_abandonModal\.open\(\)",
    }
    for name, opens in paths.items():
        source = (JS_DIR / name).read_text()
        assert re.search(opens, source), name
    assert "modal.held();" in (JS_DIR / "watch/admin.js").read_text()
    assert "letGo();" in (JS_DIR / "dl-abandon.js").read_text()
    assert "if (letGo) letGo();" in (JS_DIR / "profile-picture.js").read_text()
    admin = (JS_DIR / "watch/admin.js").read_text()
    assert "openHolding(_keepDialogModal);" in admin
