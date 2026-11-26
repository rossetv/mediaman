"""The line by the AI switch in Settings: what the switch still needs from OpenAI."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from time import monotonic

import pytest
from jinja2 import Environment, FileSystemLoader

from mediaman.app_factory import _TEMPLATE_DIR
from mediaman.services.openai import client as openai_client
from mediaman.web.routes.settings import router
from tests.helpers.factories import insert_settings

HARNESS = Path(__file__).parent / "js" / "settings_ai_need_harness.js"
NEEDS = "Needs an OpenAI key."
REFUSED = "OpenAI refused the key. Check it in the OpenAI card."


@pytest.fixture
def client(app_factory, authed_client, conn, templates_stub, monkeypatch):
    monkeypatch.setattr(openai_client, "_refused_key", None)
    app = app_factory(router, conn=conn, state_extras={"templates": templates_stub})
    return authed_client(app, conn)


def _line(client) -> tuple[bool, str, list[str]]:
    """Whether the line shows, its words and where its links go, as the page is served."""
    context = client.get("/settings").json()
    env = Environment(loader=FileSystemLoader(_TEMPLATE_DIR), autoescape=True)
    html = env.get_template("settings/_sec_general.html").render(**context)
    tag = re.search(r'<div [^>]*id="setg-ai-need"[^>]*>', html)
    assert tag, "the line's place is always on the page"
    body = html[tag.end() :].split("</p>")[0]
    words = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", body)).strip()
    return "is-open" in tag.group(0), words, re.findall(r'href="([^"]+)"', body)


def test_the_switch_on_with_no_key_says_it_needs_one_and_links_to_the_card(client) -> None:
    assert _line(client) == (True, NEEDS, ["#intg-openai"])


def test_the_switch_off_or_a_saved_key_needs_no_line(client, conn) -> None:
    insert_settings(conn, suggestions_enabled="false")
    assert _line(client)[0] is False
    conn.execute("DELETE FROM settings")
    insert_settings(conn, openai_api_key="sk-test")
    assert _line(client)[0] is False


def test_a_key_openai_refused_is_named_by_the_switch(client, conn, monkeypatch) -> None:
    insert_settings(conn, openai_api_key="sk-test", suggestions_enabled="false")
    monkeypatch.setattr(openai_client, "_refused_key", openai_client._digest("sk-test"))
    monkeypatch.setattr(openai_client, "_refused_at", monotonic())
    assert _line(client) == (True, REFUSED, ["#intg-openai"])


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_line_follows_the_switch_as_it_is_flipped_and_put_back() -> None:
    out = _run("flip")
    assert out["off"] == {"open": False, "text": NEEDS}
    assert out["on"] == {"open": True, "text": NEEDS}
    assert out["keyed"] == {"open": True, "text": NEEDS}
    assert out["discarded"]["open"] is False


def test_a_saved_key_ends_the_need_and_a_new_key_ends_the_refusal() -> None:
    out = _run("saved")
    assert out["keySaved"]["open"] is False
    assert out["otherSave"] == {"open": True, "text": REFUSED}
    assert out["newKey"]["open"] is False
    assert out["keyCleared"] == {"open": True, "text": NEEDS}


def test_the_link_opens_the_openai_card_and_moves_there() -> None:
    out = _run("link")
    assert out == {"prevented": True, "opened": 1, "focus": "openai-fold", "scrolled": True}
