"""The limits on changing users and subscribers: what counts, and how long a refusal says to wait."""

from __future__ import annotations

import pytest

from mediaman.services.rate_limit import ActionRateLimiter, limiters
from mediaman.services.rate_limit.instances import SUBSCRIBER_WRITE_LIMITER
from mediaman.web.auth.password_hash import LastAdminError
from mediaman.web.auth.reauth import grant_recent_reauth, revoke_reauth
from mediaman.web.routes.subscribers import router as subscribers_router
from mediaman.web.routes.users import (
    _PASSWORD_CHANGE_IP_LIMITER,
    _PASSWORD_CHANGE_LIMITER,
    _REAUTH_LIMITER,
    _SESSIONS_REVOKE_LIMITER,
    _UNTICKETED_LIMITER,
    _USER_CREATE_LIMITER,
    _USER_MGMT_LIMITER,
    crud,
)
from mediaman.web.routes.users import router as users_router
from tests.helpers.factories import insert_admin_user, insert_subscriber

_GOOD = "Tr1cky-Enough-Pass"
_VIC = "/api/users/{vic}"
_ME = "/api/users/{me}"


@pytest.fixture
def now(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(limiters.time, "monotonic", lambda: clock[0])
    return clock


@pytest.fixture
def send(app_factory, authed_client, conn):
    """Sends a request as an admin with, or without, a recent password check."""

    def _build(*, reauth: bool = True):
        app = app_factory(users_router, subscribers_router, conn=conn)
        client = authed_client(app, conn, with_reauth=reauth)
        vic = insert_admin_user(conn, username="vic", role="viewer")
        insert_admin_user(conn, username="casey")
        conn.execute("UPDATE admin_users SET email = 'casey@example.com' WHERE username = 'casey'")
        insert_subscriber(conn, email="reader@example.com")
        insert_subscriber(conn, email="left@example.com", active=0)
        conn.commit()
        me = conn.execute("SELECT id FROM admin_users WHERE username = 'admin'").fetchone()["id"]

        def send(method, path, **body):
            return client.request(method, path.format(vic=vic, me=me), **body)

        send.token = client.cookies["session_token"]
        return send

    return _build


def _spend(limiter: ActionRateLimiter, actor: str = "admin") -> None:
    while limiter.check(actor):
        pass


@pytest.mark.parametrize(
    ("status", "method", "path", "body"),
    [
        (400, "POST", "/api/users", {"json": {"username": "ab", "password": _GOOD}}),
        (400, "POST", "/api/users", {"json": {"username": "riley", "password": "weak"}}),
        (409, "POST", "/api/users", {"json": {"username": "casey", "password": _GOOD}}),
        (400, "PATCH", "/api/users/me/email", {"json": {"email": "nope"}}),
        (400, "PATCH", _VIC + "/email", {"json": {"email": "nope"}}),
        (409, "PATCH", _VIC + "/email", {"json": {"email": "casey@example.com"}}),
        (
            400,
            "POST",
            "/api/users/change-password",
            {"json": {"old_password": "password1234", "new_password": "weak"}},
        ),
        (
            400,
            "POST",
            "/api/users/change-password",
            {"json": {"old_password": _GOOD, "new_password": _GOOD}},
        ),
        (422, "POST", "/api/subscribers", {"data": {"email": "nope"}}),
        (409, "POST", "/api/subscribers", {"data": {"email": "reader@example.com"}}),
        (409, "POST", "/api/subscribers", {"data": {"email": "left@example.com"}}),
        (400, "DELETE", "/api/users/999", {}),
        (400, "DELETE", _ME, {}),
        (404, "PATCH", "/api/users/999/role", {"json": {"role": "admin"}}),
        (400, "PATCH", _ME + "/role", {"json": {"role": "viewer"}}),
        (404, "PATCH", "/api/users/999/email", {"json": {"email": ""}}),
        (400, "PATCH", _ME + "/email", {"json": {"email": ""}}),
        (404, "POST", "/api/users/999/unlock", {}),
        (400, "POST", _ME + "/unlock", {}),
    ],
)
def test_refused_input_uses_up_no_tries(send, status, method, path, body):
    sent = send()

    answers = {sent(method, path, **body).status_code for _ in range(12)}

    assert answers == {status}


@pytest.mark.parametrize(
    ("limiter", "method", "path", "body"),
    [
        (_USER_CREATE_LIMITER, "POST", "/api/users", {"username": "riley", "password": _GOOD}),
        (_USER_MGMT_LIMITER, "DELETE", _VIC, None),
        (_USER_MGMT_LIMITER, "PATCH", _VIC + "/role", {"role": "admin"}),
        (_USER_MGMT_LIMITER, "PATCH", _VIC + "/email", {"email": "vic@example.com"}),
        (_USER_MGMT_LIMITER, "PATCH", "/api/users/me/email", {"email": "me@example.com"}),
        (_USER_MGMT_LIMITER, "POST", _VIC + "/unlock", None),
    ],
)
def test_tries_without_a_recent_password_check_leave_the_action_s_own_and_have_their_own_limit(
    send, now, limiter, method, path, body
):
    sent = send(reauth=False)
    tries = _UNTICKETED_LIMITER._max_in_window

    answers = [sent(method, path, json=body) for _ in range(tries + 1)]

    assert {r.json()["error"] for r in answers[:tries]} == {"reauth_required"}
    assert answers[-1].status_code == 429
    assert "in 1 minute." in answers[-1].json()["message"]
    assert [limiter.check("admin") for _ in range(limiter._max_in_window + 1)] == (
        [True] * limiter._max_in_window + [False]
    )


@pytest.mark.parametrize(
    ("limiter", "method", "path", "body"),
    [
        (_USER_CREATE_LIMITER, "POST", "/api/users", {"username": "riley", "password": _GOOD}),
        (_USER_MGMT_LIMITER, "DELETE", _VIC, None),
    ],
)
def test_a_flood_without_a_recent_password_check_takes_nothing_from_one_with_it(
    send, conn, limiter, method, path, body
):
    sent = send(reauth=False)
    for _ in range(_UNTICKETED_LIMITER._max_in_window + 1):
        assert sent(method, path, json=body).status_code in (403, 429)

    grant_recent_reauth(conn, sent.token, "admin")

    assert sent(method, path, json=body).status_code == 200


def test_a_spent_limit_is_said_before_the_password_is_asked_for(send):
    sent = send(reauth=False)
    _spend(_USER_CREATE_LIMITER)

    resp = sent("POST", "/api/users", json={"username": "riley", "password": _GOOD})

    assert resp.status_code == 429


def test_adding_a_user_is_one_try_though_the_page_asks_twice_when_the_password_is_due(send, conn):
    sent = send(reauth=False)
    answers = []
    for name in ("riley", "jules", "robin"):
        body = {"username": name, "password": _GOOD}
        answers.append(sent("POST", "/api/users", json=body).status_code)
        sent("POST", "/api/auth/reauth", json={"password": "password1234"})
        answers.append(sent("POST", "/api/users", json=body).status_code)
        revoke_reauth(conn, sent.token)

    assert answers == [403, 200] * 3
    grant_recent_reauth(conn, sent.token, "admin")
    fourth = sent("POST", "/api/users", json={"username": "sasha", "password": _GOOD})
    assert fourth.status_code == 429


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [("DELETE", _VIC, {}), ("PATCH", _VIC + "/role", {"json": {"role": "viewer"}})],
)
def test_the_refusal_to_lose_the_last_admin_uses_up_no_tries(send, monkeypatch, method, path, body):
    sent = send()

    def refuse(*_args, **_kwargs):
        raise LastAdminError

    monkeypatch.setattr(crud, "is_last_admin", lambda *_args: True)
    monkeypatch.setattr(crud, "set_user_role", refuse)

    answers = {sent(method, path, **body).json()["error"] for _ in range(12)}

    assert answers == {"last_admin"}


def test_asking_whether_an_address_belongs_to_someone_else_counts(send):
    sent = send()

    answers = [
        sent("PATCH", "/api/users/me/email", json={"email": "casey@example.com"}).status_code
        for _ in range(_USER_MGMT_LIMITER._max_in_window + 1)
    ]

    assert answers[-2:] == [400, 429]


def test_a_wrong_current_password_counts(send):
    sent = send()
    body = {"old_password": "wrong", "new_password": _GOOD}

    answers = [
        sent("POST", "/api/users/change-password", json=body).status_code
        for _ in range(_PASSWORD_CHANGE_LIMITER._max_in_window + 1)
    ]

    assert answers[-2:] == [403, 429]


@pytest.mark.parametrize(
    ("limiter", "method", "path", "body"),
    [
        (
            _USER_CREATE_LIMITER,
            "POST",
            "/api/users",
            {"json": {"username": "riley", "password": _GOOD}},
        ),
        (_USER_MGMT_LIMITER, "DELETE", _VIC, {}),
        (_USER_MGMT_LIMITER, "PATCH", _VIC + "/role", {"json": {"role": "admin"}}),
        (_USER_MGMT_LIMITER, "PATCH", _VIC + "/email", {"json": {"email": ""}}),
        (_USER_MGMT_LIMITER, "PATCH", "/api/users/me/email", {"json": {"email": ""}}),
        (_USER_MGMT_LIMITER, "POST", _VIC + "/unlock", {}),
        (
            _PASSWORD_CHANGE_LIMITER,
            "POST",
            "/api/users/change-password",
            {"json": {"old_password": "x", "new_password": _GOOD}},
        ),
        (_REAUTH_LIMITER, "POST", "/api/auth/reauth", {"json": {"password": "x"}}),
        (_SESSIONS_REVOKE_LIMITER, "POST", "/api/users/sessions/revoke-others", {}),
        (
            SUBSCRIBER_WRITE_LIMITER,
            "POST",
            "/api/subscribers",
            {"data": {"email": "a@example.com"}},
        ),
        (SUBSCRIBER_WRITE_LIMITER, "DELETE", "/api/subscribers/1", {}),
    ],
)
def test_a_limit_says_how_long_to_wait(send, now, limiter, method, path, body):
    sent = send()
    _spend(limiter)
    now[0] += limiter._window - 50

    resp = sent(method, path, **body)

    assert (resp.status_code, resp.json()["error"]) == (429, "too_many_requests")
    assert "in 50 seconds" in resp.json()["message"]
    assert resp.headers["Retry-After"] == "50"


def test_the_limit_on_a_network_says_how_long_to_wait_too(send, now):
    sent = send()
    _spend(_PASSWORD_CHANGE_IP_LIMITER, "testclient")

    resp = sent(
        "POST", "/api/users/change-password", json={"old_password": "x", "new_password": _GOOD}
    )

    assert "in 1 minute." in resp.json()["message"]


@pytest.mark.parametrize(
    ("later", "said"),
    [(0, "1 hour"), (1, "1 hour"), (61, "59 minutes"), (3540, "1 minute"), (3599, "1 second")],
)
def test_a_wait_is_said_in_the_unit_that_suits_it_and_never_too_short(send, now, later, said):
    sent = send()
    _spend(_USER_CREATE_LIMITER)
    now[0] += later

    resp = sent("POST", "/api/users", json={"username": "riley", "password": _GOOD})

    assert f"in {said}." in resp.json()["message"]


def test_the_wait_after_the_day_s_last_try_is_until_the_first_is_a_day_old(now):
    limiter = ActionRateLimiter(max_in_window=2, window_seconds=60, max_per_day=3)
    for _ in range(3):
        assert limiter.check("a")
        now[0] += 100

    assert limiter.retry_after("a") == 86_400 - 300
    now[0] += 86_400 - 300 - 1
    assert not limiter.check("a")
    now[0] += 1.5
    assert limiter.check("a")


def test_taking_a_try_back_frees_that_one_and_no_other(now):
    limiter = ActionRateLimiter(max_in_window=2, window_seconds=60, max_per_day=2)
    early = limiter.reserve("a")
    now[0] += 50
    assert limiter.check("a")
    assert limiter.reserve("a") is None

    limiter.release("a", early)
    limiter.release("a", early)

    # the later one still counts, for the whole of its own minute and day
    assert [limiter.check("a"), limiter.check("a")] == [True, False]
    now[0] += 59
    assert not limiter.check("a")
