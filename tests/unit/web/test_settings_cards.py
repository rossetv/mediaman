"""Settings' integration cards and the buttons around them: names, words and folds.

The scripts run under node against the fake DOM (js/settings_cards_harness.js); the
markup is the real template rendered; the styles are read from the sheets.
"""

from __future__ import annotations

import json
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import jinja2
import pytest

from tests.unit.web._css import px, rules, style

WEB = Path(__file__).resolve().parents[3] / "src/mediaman/web"
JS = Path(__file__).parent / "js"
PANELS = "_settings_pg_panels.css"
_VOID = {"input", "br", "img", "hr", "meta", "link", "use"}


def _node(harness: str, scenario: str, *args: str):
    result = subprocess.run(
        ["node", str(JS / harness), scenario, *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(result.stdout)


def _cards(scenario: str, *args: str):
    return _node("settings_cards_harness.js", scenario, *args)


def _integrations() -> str:
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(WEB / "templates"), autoescape=True)
    return env.get_template("settings/_sec_integrations.html").render(settings={})


class _Tree(HTMLParser):
    """Each element with its attributes and the elements open around it."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[dict[str, str | None]] = []
        self.seen: list[tuple[dict[str, str | None], list[dict[str, str | None]]]] = []

    def handle_starttag(self, tag, attrs):
        el = {"tag": tag, **dict(attrs)}
        self.seen.append((el, list(self.stack)))
        if tag not in _VOID:
            self.stack.append(el)

    def handle_endtag(self, tag):
        if tag not in _VOID and self.stack:
            self.stack.pop()

    def find(self, test):
        return [(el, around) for el, around in self.seen if test(el)]


def _tree() -> _Tree:
    tree = _Tree()
    tree.feed(_integrations())
    return tree


def _has_class(el, name: str) -> bool:
    return name in (el.get("class") or "").split()


# The TMDB card


def test_the_tmdb_card_asks_only_for_the_read_token_the_app_uses() -> None:
    html = _integrations()
    assert 'id="tmdb_api_key"' not in html
    assert 'id="tmdb_read_token"' in html


def test_a_save_no_longer_sends_the_tmdb_api_key() -> None:
    keys = _node("settings_harness.js", "payloadKeys")
    assert "tmdb_read_token" in keys
    assert "tmdb_api_key" not in keys


# Sync now


def test_sync_now_says_how_many_items_synced() -> None:
    out = _cards("sync", json.dumps({"ok": True, "synced": 1204, "errors": 0, "removed": 0}))
    assert out["shown"] == "Synced ✓"
    assert [kind for kind, _ in out["said"]] == ["success"]
    assert "1,204 items" in out["said"][0][1]
    assert out["after"] == "Sync now"


def test_sync_now_with_nothing_synced_says_so_and_where_to_choose_libraries() -> None:
    out = _cards("sync", json.dumps({"ok": True, "synced": 0, "errors": 0, "removed": 0}))
    assert out["shown"] == "0 synced"
    (kind, words), *_ = out["said"]
    assert kind != "success"
    assert "Nothing synced" in words and "Libraries & Paths" in words


@pytest.mark.parametrize(("synced", "start"), [(10, "Synced 10 items"), (0, "Nothing synced")])
def test_sync_now_counts_what_failed(synced: int, start: str) -> None:
    out = _cards("sync", json.dumps({"ok": True, "synced": synced, "errors": 3, "removed": 0}))
    assert out["shown"] == "3 failed"
    assert out["said"][0][0] == "error"
    assert out["said"][0][1].startswith(start) and "3 failed" in out["said"][0][1]


# The cards' headers and buttons


def test_no_card_header_is_a_button_holding_another_button() -> None:
    tree = _tree()
    buttons = tree.find(lambda el: el["tag"] == "button" or el.get("role") == "button")
    assert buttons
    for _, around in buttons:
        assert not any(a["tag"] == "button" or a.get("role") == "button" for a in around)


def test_each_cards_title_is_the_button_that_folds_it() -> None:
    tree = _tree()
    folds = tree.find(lambda el: _has_class(el, "intg-fold"))
    assert len(folds) == 8
    for fold, around in folds:
        assert fold["tag"] == "button" and fold.get("aria-expanded") == "false"
        assert fold["aria-controls"].startswith("intg-body-")
        assert around[-1]["tag"] == "h3"
    assert tree.find(lambda el: el.get("id") == "intg-body-sonarr")


def test_each_test_button_is_named_for_its_service() -> None:
    names = {
        el["data-test-service"]: el.get("aria-label")
        for el, _ in _tree().find(lambda el: "data-test-service" in el)
    }
    assert names["sonarr"] == "Test Sonarr"
    assert names["mailgun"] == "Test Email (Mailgun)"
    assert len(names) == 8


def test_a_test_buttons_name_follows_its_words() -> None:
    out = _cards("testNames")
    assert out["painted"] == [["Test", "Test Sonarr"], ["Test saved", "Test saved Radarr settings"]]
    # While it works it is named by what it shows.
    assert out["testing"] == ["Testing…", None]
    # A repaint while it works leaves it as it is.
    assert out["repainted"] == out["testing"]


def test_a_reveal_button_says_what_it_shows_and_hides() -> None:
    assert _cards("reveal") == [
        ["Hide", "Hide Plex API Key", "text"],
        ["Show", "Show Plex API Key", "password"],
    ]


def test_the_title_and_the_rest_of_the_header_fold_the_card_but_test_does_not() -> None:
    out = _cards("fold")
    assert out["button"] == [True, "true"]
    assert out["header"] == [False, "false"]
    assert out["test"] == out["answer"] == [False, "false"]
    assert out["chevron"] == [True, "true"]
    assert len(out["folds"]) == 3


def test_the_ring_of_the_fold_button_goes_round_the_whole_header() -> None:
    ring = style(".setg-pg .intg-card-hd:has(.intg-fold:focus-visible)", PANELS)["outline"].split()
    assert px(ring[0]) >= 2 and ring[1] == "solid"
    # the title draws none of its own inside it
    assert style(".setg-pg .intg-fold:focus-visible", PANELS)["outline"] in ("0", "none")


# Folds and danger buttons


def test_a_fold_does_not_ease_its_border_width() -> None:
    eased = {
        part.split()[0] for part in style(".setg-pg .setg-fold", PANELS)["transition"].split(",")
    }
    shut = style(".setg-pg .setg-fold.is-shut", PANELS)
    assert "grid-template-rows" in shut and "grid-template-rows" in eased
    # a shut fold drops its border width, and that width is not among what eases
    assert any(prop.startswith("border") for prop in shut)
    assert "all" not in eased and not any(prop.startswith("border") for prop in eased)


def test_remove_and_delete_keep_a_border_in_forced_colours() -> None:
    edge = style(".setg-pg .link-danger", PANELS, "(forced-colors: active)")["border"].split()
    assert px(edge[0]) >= 1 and edge[1] == "solid"


def test_the_list_says_how_a_service_stands_in_a_word_while_it_is_tested() -> None:
    out = _cards("answers")
    assert out["testing"] == ["Testing…", None]


def test_an_open_card_says_the_whole_answer_above_its_fields_and_the_single_pill_span_keeps_a_word() -> (
    None
):
    out = _cards("answers")
    long = "The service refused the request: check the API key and the address."
    assert out["err"] == ["Not answering", long]
    assert out["warn"] == ["Rate limited", None]
    assert out["ok"] == ["Connected", None]
    assert out["off"] == ["Not set up", None]


def test_a_card_with_edits_offers_save_beside_its_test_and_a_clean_one_does_not() -> None:
    out = _cards("saveInCard")
    assert out["sonarr"] is None
    assert out["radarr"] == ["Save", "intg-acts", "radarr"]
    assert out["saved"] == 1


def test_once_saved_the_cards_save_goes_and_the_focus_moves_on_to_test() -> None:
    out = _cards("saveInCard")
    assert out["after"] is None
    assert out["focus"] == "radarr"


def test_an_open_cards_fields_run_in_one_column_to_its_edges() -> None:
    tracks = style(".setg-pg .intg-card-bd .fld-row", PANELS)["grid-template-columns"]
    assert len(re.findall(r"\w+\([^)]*\)|\S+", tracks)) == 1
    # every width cap a field or its wrap carries elsewhere is lifted inside a card; a count
    # stays as short as its number
    shapes = {
        name
        for el, _ in _tree().find(
            lambda el: el["tag"] in ("input", "select", "div") and el.get("type") != "number"
        )
        for name in (el.get("class") or "").split()
        if name.startswith("inp")
    }
    capped = {
        name
        for name in shapes
        for _, sels, decls in rules("_settings_pg.css")
        if f".setg-pg .{name}" in sels and decls.get("max-width", "none") != "none"
    }
    assert {"inp--lg", "inp-wrap"} <= capped
    for name in capped:
        assert style(f".setg-pg .intg-card-bd .{name}", PANELS)["max-width"] == "none", name


def _inline(padding: str) -> str:
    """The inline value of a padding shorthand."""
    sides = padding.split()
    return sides[1] if len(sides) > 1 else sides[0]


def test_the_badge_the_fields_and_the_dividers_share_one_gutter() -> None:
    header = _inline(style(".setg-pg .intg-card-hd", PANELS)["padding"])
    body = _inline(style(".setg-pg .intg-card-bd > .setg-fold-inner", PANELS)["padding"])
    divider = style(".setg-pg .intg-card + .intg-card::before", PANELS)["left"]
    assert header == body == divider
    # a narrow screen gives the row more height and keeps the gutter
    narrow = style(".setg-pg .intg-card-hd", PANELS, "(max-width: 520px)")
    assert "padding-block" in narrow
    assert not {"padding", "padding-inline", "padding-left", "padding-right"} & set(narrow)
