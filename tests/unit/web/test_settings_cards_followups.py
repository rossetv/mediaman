"""Settings' integration cards: what a screen reader hears, and what holds still."""

from __future__ import annotations

import json
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import jinja2

from mediaman.services.infra.settings_reader import get_setting
from mediaman.web.routes.settings import router

WEB = Path(__file__).resolve().parents[3] / "src/mediaman/web"
PANELS = (WEB / "static/css/_settings_pg_panels.css").read_text(encoding="utf-8")


def _integrations() -> str:
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(WEB / "templates"), autoescape=True)
    return env.get_template("settings/_sec_integrations.html").render(settings={})


class _Elements(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.els: list[dict[str, str | None]] = []

    def handle_starttag(self, tag, attrs):
        self.els.append({"tag": tag, **dict(attrs)})


def _elements() -> list[dict[str, str | None]]:
    parser = _Elements()
    parser.feed(_integrations())
    return parser.els


def _classes(el) -> list[str]:
    return (el.get("class") or "").split()


# The fold button's focus ring


def test_the_focused_headers_ring_takes_the_system_focus_colour_in_forced_colours() -> None:
    blocks = re.findall(r"@media \(forced-colors: active\) \{(.*?)\n\}", PANELS, re.S)
    assert any(
        re.search(
            r"\.setg-pg \.intg-card-hd:has\(\.intg-fold:focus-visible\) \{[^}]*outline-color: Highlight",
            block,
        )
        for block in blocks
    )


# Sync now holds its width


def test_sync_now_keeps_one_width_through_its_labels() -> None:
    # "Syncing…" is the widest label, about 111px at 12px type.
    rule = re.search(r"\.setg-pg #btn-sync-library \{([^}]*)\}", PANELS)
    assert rule
    width = re.search(r"min-width: ([\d.]+)em", rule.group(1))
    assert width and float(width.group(1)) * 12 >= 111


# The cards' names


def test_a_reveal_button_never_says_a_service_twice() -> None:
    names = [el["aria-label"] for el in _elements() if "inp-reveal" in _classes(el)]
    assert "Show Mailgun API Key" in names
    assert "Show Plex Token" in names
    assert "Show Sonarr API Key" in names
    assert "Show TMDB API Read Token" in names
    for name in names:
        words = name.lower().split()
        assert len(words) == len(set(words)), name


def test_a_field_is_named_by_the_same_words_as_its_reveal_button() -> None:
    els = _elements()
    fields = {el["id"]: el["aria-label"] for el in els if el.get("id") and el.get("aria-label")}
    for el in els:
        if "inp-reveal" in _classes(el):
            assert el["aria-label"] == "Show " + fields[el["data-reveal-target"]]


def test_each_fold_button_is_described_by_its_cards_subtitle() -> None:
    els = _elements()
    ids = [el["id"] for el in els if el.get("id")]
    assert len(ids) == len(set(ids))
    subs = {el["id"]: el for el in els if el.get("id") and "sub" in _classes(el)}
    folds = [el for el in els if "intg-fold" in _classes(el)]
    assert len(folds) == 8
    for fold in folds:
        assert fold["aria-describedby"] in subs


# What a save keeps


def test_a_save_without_the_tmdb_api_key_keeps_the_one_stored(
    app_factory, authed_client, conn, secret_key
) -> None:
    client = authed_client(app_factory(router, conn=conn), conn, with_reauth=True)
    assert client.put("/api/settings", json={"tmdb_api_key": "stored-v3-key"}).status_code == 200
    # What the page sends now: no such field.
    assert client.put("/api/settings", json={"tmdb_read_token": "read-token"}).status_code == 200
    assert get_setting(conn, "tmdb_api_key", secret_key=secret_key) == "stored-v3-key"
    assert get_setting(conn, "tmdb_read_token", secret_key=secret_key) == "read-token"


# Sync now's toasts


def _sync_toasts(reply: dict | str) -> list[dict]:
    harness = Path(__file__).parent / "js/settings_sync_toast_harness.js"
    out = subprocess.run(
        ["node", str(harness), json.dumps(reply)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(out.stdout)


def test_the_toast_that_asks_for_libraries_stays_long_enough_to_act_on() -> None:
    (toast,) = _sync_toasts({"ok": True, "synced": 0, "errors": 0, "removed": 0})
    assert "Libraries & Paths" in toast["message"]
    assert toast["duration"] >= 8000


def test_the_toasts_of_a_failed_sync_stay_long_enough_to_read() -> None:
    (some,) = _sync_toasts({"ok": True, "synced": 3, "errors": 2, "removed": 0})
    assert some["kind"] == "error" and some["duration"] >= 8000
    (none,) = _sync_toasts({"ok": True, "synced": 0, "errors": 2, "removed": 0})
    assert none["kind"] == "error" and none["duration"] >= 8000
    (lost,) = _sync_toasts("reject")
    assert lost["kind"] == "error" and lost["duration"] >= 8000
