"""Signing in at plex.tv: who may link, and that the token always goes."""

from __future__ import annotations

import pytest
import requests

from mediaman.services.plex_link.plex_tv import (
    PinExpired,
    PlexTv,
    PlexTvUnavailable,
    SignInToken,
)
from tests.helpers.fake_plex import SERVER_ID, TOKEN, FakePlex

TV = "https://plex.tv/api/v2"


@pytest.fixture
def plex(monkeypatch):
    return FakePlex().install(monkeypatch)


@pytest.fixture
def tv():
    return PlexTv("client-1", "mediaman (watch.example.com)")


def _use(tv):
    return tv.use_sign_in(SignInToken(TOKEN), SERVER_ID)


def test_a_friend_is_known_by_their_plex_tv_id(plex, tv):
    result = _use(tv)
    assert (result.identity.plex_account_id, result.identity.server_account_id) == (4242, 4242)
    assert result.identity.name == "amyplex"


def test_the_owner_is_account_one_on_the_server(plex, tv):
    plex.resource["owned"] = True
    identity = _use(tv).identity
    assert (identity.plex_account_id, identity.server_account_id) == (4242, 1)


@pytest.mark.parametrize(
    ("resource", "user", "reason"),
    [
        ({"clientIdentifier": "another"}, {}, "not_on_server"),
        ({"provides": "player"}, {}, "not_on_server"),
        ({"owned": True, "home": True}, {}, "unsupported_account"),
        ({"owned": True, "home": None}, {}, "unsupported_account"),
        ({"owned": None}, {}, "unsupported_account"),
        ({}, {"restricted": True}, "unsupported_account"),
        ({}, {"restricted": None}, "unsupported_account"),
        ({}, {"id": 1}, "unsupported_account"),
        ({}, {"id": "amy"}, "unsupported_account"),
    ],
)
def test_an_account_that_cannot_be_placed_is_refused(plex, tv, resource, user, reason):
    plex.resource.update(resource)
    plex.user.update(user)
    result = _use(tv)
    assert (result.identity, result.reason) == (None, reason)
    assert plex.signed_out() == 1


@pytest.mark.parametrize(
    ("path", "answer", "reason"),
    [
        ("/user", (401, "{}"), "signin_rejected"),
        ("/user", (429, "{}"), "plex_unavailable"),
        ("/resources", (500, "{}"), "plex_unavailable"),
        ("/resources", (302, "{}"), "plex_unavailable"),
        ("/resources", (200, "not json"), "plex_unavailable"),
        ("/resources", (200, "[" * 500_000), "plex_unavailable"),
        ("/resources", (200, {"a": "list was expected"}), "plex_unavailable"),
        ("/user", requests.ConnectionError("down"), "plex_unavailable"),
    ],
)
def test_the_token_is_signed_out_when_the_sign_in_cannot_be_read(plex, tv, path, answer, reason):
    plex.answers["GET", TV + path] = answer
    result = _use(tv)
    assert (result.identity, result.reason) == (None, reason)
    assert plex.signed_out() == 1


def test_a_failed_sign_out_is_reported_and_keeps_the_identity(plex, tv):
    plex.answers["DELETE", f"{TV}/users/signout"] = (500, "{}")
    result = _use(tv)
    assert result.identity is not None
    assert result.signed_out is False


def test_the_token_travels_in_a_header_to_plex_tv_alone_and_is_never_logged(plex, tv, caplog):
    caplog.set_level("DEBUG")
    plex.answers["GET", f"{TV}/resources"] = (500, {"echo": TOKEN})
    result = _use(tv)
    assert TOKEN not in caplog.text
    assert TOKEN not in repr(result)
    assert TOKEN not in repr(SignInToken(TOKEN))
    for _, where, headers, query in plex.calls:
        assert where.startswith(f"{TV}/")
        assert headers["X-Plex-Token"] == TOKEN
        assert TOKEN not in where + str(query)


def test_a_pin_lives_ten_minutes_at_most_and_its_code_stays_in_the_link(plex, tv):
    pin = tv.create_pin()
    assert pin.expires_in_s == 600
    url = tv.approval_url(pin)
    assert url.startswith("https://app.plex.tv/auth#?clientID=client-1&code=strongcode&")
    assert "watch.example.com" in url
    assert "forwardUrl" not in url


@pytest.mark.parametrize(
    "answer",
    [
        (201, {"id": 77, "code": "a b&c=d", "expiresIn": 60}),
        (201, {"id": "77", "code": "ok", "expiresIn": 60}),
        (201, []),
        (429, "{}"),
        requests.Timeout("slow"),
    ],
)
def test_a_pin_that_cannot_be_read_is_plex_being_down(plex, tv, answer):
    plex.answers["POST", f"{TV}/pins"] = answer
    with pytest.raises(PlexTvUnavailable):
        tv.create_pin()


def test_only_a_404_ends_a_pin(plex, tv):
    pin = tv.create_pin()
    assert tv.poll_pin(pin) is None
    plex.answers["GET", f"{TV}/pins/77"] = (500, "{}")
    with pytest.raises(PlexTvUnavailable):
        tv.poll_pin(pin)
    plex.answers["GET", f"{TV}/pins/77"] = (404, "{}")
    with pytest.raises(PinExpired):
        tv.poll_pin(pin)


@pytest.mark.parametrize(("status", "signed_out"), [(401, True), (403, False), (429, False)])
def test_a_token_plex_no_longer_accepts_is_signed_out(plex, tv, status, signed_out):
    plex.answers["DELETE", f"{TV}/users/signout"] = (status, "{}")
    assert tv.sign_out(SignInToken(TOKEN)) is signed_out
