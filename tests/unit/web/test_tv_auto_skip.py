"""Skip intros and credits on the TV: the person's setting, kept, shown in Profile and sent to
their TV with what it plays."""

from __future__ import annotations

import re

import pytest
from fastapi.templating import Jinja2Templates

from mediaman.app_factory import _TEMPLATE_DIR
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.web.repository.track_choices import set_skips_on_tv
from mediaman.web.routes.account import router as account_router
from mediaman.web.routes.watch import tracks_api
from tests.unit.web.test_tv import (  # noqa: F401 - the fixtures of a TV against a fake Plex
    _play,
    _tv_of,
    amy,
    app,
    clock,
    home,
    lookups,
    manager,
    person,
    plex,
    tv,
)


@pytest.fixture
def playback(app_factory, authed_client, conn, monkeypatch):
    monkeypatch.setattr(tracks_api, "_LIMITER", ActionRateLimiter(60, 60))
    return authed_client(app_factory(tracks_api.router, conn=conn), conn, username="alice")


def test_the_setting_is_on_until_the_person_turns_it_off_and_keeps_their_languages(playback):
    assert playback.get("/api/account/playback").json()["auto_skip"] is True
    playback.put("/api/account/playback", json={"audio_lang": "de"})
    resp = playback.put("/api/account/playback", json={"auto_skip": False})
    assert (resp.json()["auto_skip"], resp.json()["audio_lang"]) == (False, "de")
    assert playback.get("/api/account/playback").json()["auto_skip"] is False
    # A change of language leaves it as it is.
    playback.put("/api/account/playback", json={"subtitle_lang": "off"})
    assert playback.get("/api/account/playback").json()["auto_skip"] is False


@pytest.mark.parametrize("value", [None, "no", 0])
def test_a_setting_that_is_not_on_or_off_is_refused_and_nothing_changes(playback, value):
    assert playback.put("/api/account/playback", json={"auto_skip": value}).status_code == 422
    assert playback.get("/api/account/playback").json()["auto_skip"] is True


@pytest.mark.parametrize("role", ["admin", "viewer"])
def test_profile_shows_the_switch_as_set_to_admins_and_viewers(
    app_factory, authed_client, viewer_client, conn, role
):
    templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
    pane = app_factory(account_router, conn=conn, state_extras={"templates": templates})
    client = (authed_client if role == "admin" else viewer_client)(pane, conn, username="rosa")

    def switch() -> str:
        html = client.get("/account?pane=1").text
        found = re.search(r'<button[^>]*id="account-skip"[^>]*>', html)
        assert found, "no switch"
        return found.group(0)

    assert 'aria-checked="true"' in switch() and "tog on" in switch()
    set_skips_on_tv(conn, "rosa", False)
    assert 'aria-checked="false"' in switch() and "tog on" not in switch()


def test_what_plays_on_the_tv_carries_the_setting_of_whoever_started_it(tv, home, amy, conn):  # noqa: F811
    assert _play(amy, home).status_code == 200
    assert tv.poll()["play"]["auto_skip"] is True
    set_skips_on_tv(conn, "amy", False)
    # It holds from the next title played; the remote reads the same.
    assert tv.poll()["play"]["auto_skip"] is True
    assert _play(amy, home, rating_key="102").status_code == 200
    assert tv.poll()["play"]["auto_skip"] is False
    assert _tv_of(amy, home)["play"]["auto_skip"] is False
