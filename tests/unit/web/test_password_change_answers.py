"""What changing a password answers: the field at fault, the rules refused, the lock, the devices."""

from __future__ import annotations

import json

import pytest

from mediaman.db import set_connection
from mediaman.main import create_app
from mediaman.services.rate_limit import reset_all_limiters
from mediaman.web.auth.login_lockout import reauth_key, record_failure
from mediaman.web.auth.password_hash import _change_password as rotation
from mediaman.web.auth.password_hash import (
    change_password,
    delete_user,
    set_must_change_password,
    verify_password,
)
from mediaman.web.auth.password_hash._change_password import password_changed_at
from mediaman.web.auth.password_policy import password_issues
from mediaman.web.auth.session_store import create_session, list_sessions_for, validate_session
from mediaman.web.repository import tv as tvs
from mediaman.web.routes.users.passwords import rules_of
from tests.helpers.factories import insert_admin_user

_ROUTE = "/api/users/change-password"
_PAGE = "/force-password-change"
_OLD = "Copper-lantern-19"
_NEW = "Harbour-glass-27"
_NOW = "2026-03-14T09:26:53+00:00"


@pytest.fixture(autouse=True)
def _cheap_hashes(monkeypatch):
    monkeypatch.setattr(rotation, "BCRYPT_ROUNDS", 4)
    monkeypatch.setattr("mediaman.web.auth.password_hash._user_crud.BCRYPT_ROUNDS", 4)


@pytest.fixture
def client(conn, viewer_client, freezer):
    freezer.move_to(_NOW)
    reset_all_limiters()
    set_connection(conn)
    insert_admin_user(conn, username="boss")
    client = viewer_client(create_app(), conn, password=_OLD)
    client.headers.update({"origin": "http://testserver"})
    return client


@pytest.fixture
def page(client, conn, templates_stub):
    """The same person, made to choose a new password; the page answers with what it is given."""
    set_must_change_password(conn, "viewer", True)
    client.app.state.templates = templates_stub
    return client


def _change(client, old=_OLD, new=_NEW):
    reset_all_limiters()
    return client.post(_ROUTE, json={"old_password": old, "new_password": new})


def _submit(client, old=_OLD, new=_NEW, again=None):
    reset_all_limiters()
    data = {"old_password": old, "new_password": new}
    data["confirm_password"] = new if again is None else again
    return client.post(_PAGE, data=data, follow_redirects=False)


def _lock(conn, tries=5):
    for _ in range(tries):
        record_failure(conn, reauth_key("viewer"))


def _pair(conn, username, name):
    owner = conn.execute("SELECT id FROM admin_users WHERE username = ?", (username,)).fetchone()
    conn.execute(
        "INSERT INTO tvs (owner_id, kind, name, token_hash, created_at) VALUES (?, 'page', ?, ?, ?)",
        (owner["id"], name, f"hash of {username} {name}", _NOW),
    )
    conn.commit()


class TestProfileErrors:
    def test_a_wrong_current_password_is_said_about_its_field(self, client):
        resp = _change(client, old="not it at all")

        assert resp.status_code == 403
        assert resp.json() == {
            "ok": False,
            "error": "wrong_password",
            "message": "Current password is incorrect",
            "field": "old_password",
            "locked": False,
        }

    def test_each_refused_rule_is_named(self, client):
        body = _change(client, new="viewerviewer").json()

        assert (body["error"], body["field"]) == ("weak_password", "new_password")
        assert body["rules"] == ["repeats", "username", "mix"]
        assert len(body["issues"]) == 3

    def test_the_same_password_is_a_named_rule_however_it_is_encoded(self, client, conn):
        composed, decomposed = "Café-lantern-19", "Café-lantern-19"

        resp = _change(client, old=composed, new=decomposed)

        assert resp.status_code == 400
        assert resp.json()["error"] == "same_password"
        assert (resp.json()["field"], resp.json()["rules"]) == ("new_password", ["same"])

    def test_every_rule_of_the_policy_has_a_name(self):
        refused = ["", "x" * 1025, "aaaa", "viewer-Viewer-12", "abcdefghijkl", "1qaz2wsx"]
        texts = [text for pw in refused for text in password_issues(pw, "viewer")]

        assert set(rules_of(texts)) == {
            "required",
            "too_long",
            "length",
            "repeats",
            "username",
            "common",
            "mix",
            "sequence",
        }


class TestTheLock:
    def test_the_fifth_wrong_try_says_it_is_locked_and_for_how_long(self, client):
        answers = [_change(client, old=f"wrong {n}") for n in range(5)]

        assert [a.json()["locked"] for a in answers] == [False] * 4 + [True]
        assert {a.status_code for a in answers} == {403}
        assert {a.json()["error"] for a in answers} == {"wrong_password"}
        assert answers[4].json()["retry_after"] == 15 * 60
        assert "Try again in 15 minutes." in answers[4].json()["message"]

    def test_what_is_left_of_the_lock_is_said(self, client, conn, freezer):
        _lock(conn)
        freezer.tick(10 * 60)

        assert _change(client, old="wrong").json()["retry_after"] == 5 * 60

    @pytest.mark.parametrize(("before", "wait"), [(5, 15 * 60), (9, 60 * 60), (14, 24 * 60 * 60)])
    def test_while_locked_the_right_password_is_answered_like_a_wrong_one(
        self, client, conn, before, wait
    ):
        _lock(conn, before)
        right = _change(client)
        conn.execute("DELETE FROM login_failures")
        conn.commit()
        _lock(conn, before)
        wrong = _change(client, old="wrong")

        assert right.json() == wrong.json()
        assert (right.status_code, right.headers.get("set-cookie")) == (403, None)
        assert right.json()["retry_after"] == wait
        assert verify_password(conn, "viewer", _OLD)

    def test_a_lock_whose_end_cannot_be_read_gives_no_time(self, client, conn):
        _lock(conn, 1)
        conn.execute("UPDATE login_failures SET locked_until = 'soon'")
        conn.commit()

        body = _change(client).json()

        assert (body["locked"], body["retry_after"]) == (True, None)
        assert verify_password(conn, "viewer", _OLD)

    def test_the_right_password_works_again_after_it(self, client, conn, freezer):
        _lock(conn)
        freezer.tick(15 * 60 + 1)

        assert _change(client).status_code == 200

    def test_nobody_without_the_session_learns_of_it(self, client, conn):
        client.cookies.clear()

        def seen():
            login = {"username": "viewer", "password": "wrong"}
            tries = [
                _change(client),
                _submit(client),
                client.post("/login", data=login, follow_redirects=False),
            ]
            return [(t.status_code, t.text) for t in tries]

        before = seen()
        _lock(conn)

        assert seen() == before
        assert before[0][0] == 401

    def test_another_person_is_told_nothing_of_it(self, client, conn):
        _lock(conn)
        client.cookies.set("session_token", create_session(conn, "boss"))

        assert _change(client, old="wrong").json()["locked"] is False


class TestDone:
    def test_the_answer_names_the_tvs_unpaired_and_counts_the_devices_signed_out(
        self, client, conn
    ):
        others = [create_session(conn, "viewer") for _ in range(2)]
        expired = create_session(conn, "viewer")
        conn.execute(
            "UPDATE admin_sessions SET expires_at = '2026-03-14T09:26:53+00:00'"
            " WHERE public_id = ?",
            (
                next(
                    s["public_id"]
                    for s in list_sessions_for(conn, "viewer", current_token=expired)
                    if s["current"]
                ),
            ),
        )
        create_session(conn, "boss")
        _pair(conn, "viewer", "Living room")
        _pair(conn, "viewer", "Loft")
        _pair(conn, "boss", "Office")

        body = _change(client).json()

        assert (body["signed_out"], body["unpaired"]) == (2, ["Living room", "Loft"])
        assert tvs.owned(conn, "viewer") == []
        assert [tv.name for tv in tvs.owned(conn, "boss")] == ["Office"]
        assert [validate_session(conn, token) for token in others] == [None, None]

    def test_with_no_other_device_it_says_so(self, client):
        body = _change(client).json()

        assert (body["ok"], body["signed_out"], body["unpaired"]) == (True, 0, [])


class TestLastChanged:
    def _given(self, client, templates_stub):
        client.app.state.templates = templates_stub
        return client.get("/account").json()["password_changed"]

    def test_profile_says_nothing_before_the_first_change(self, client, templates_stub):
        assert self._given(client, templates_stub) is None

    @pytest.mark.parametrize("event", ["password.changed", "password.forced_changed"])
    def test_profile_gives_the_day_of_the_last_change(
        self, client, conn, freezer, templates_stub, event
    ):
        _change(client)
        freezer.move_to("2026-04-02T08:00:00+00:00")
        change_password(
            conn, "viewer", _NEW, _OLD, audit_actor="viewer", audit_ip="-", audit_event=event
        )
        client.cookies.set("session_token", create_session(conn, "viewer"))

        assert self._given(client, templates_stub) == {
            "at": "2026-04-02T08:00:00+00:00",
            "text": "2 April 2026",
        }

    def test_a_refused_change_is_not_one(self, client, conn):
        _change(client, old="wrong")

        assert password_changed_at(conn, "viewer") is None

    def test_another_person_s_change_is_not_one(self, client, conn):
        _change(client)

        assert password_changed_at(conn, "boss") is None

    def test_a_removed_account_s_change_is_not_that_of_the_next_of_its_name(
        self, client, conn, freezer
    ):
        _change(client)
        viewer = conn.execute("SELECT id FROM admin_users WHERE username = 'viewer'").fetchone()
        conn.execute(
            "INSERT INTO audit_log (media_item_id, action, created_at, actor)"
            " VALUES ('_security', 'sec:password.changed', ?, 'viewer')",
            (_NOW,),
        )
        conn.commit()
        assert delete_user(conn, viewer["id"], "boss")
        freezer.tick(60)
        insert_admin_user(conn, username="viewer", role="viewer")

        assert password_changed_at(conn, "viewer") is None

    def test_a_change_recorded_without_the_account_s_id_counts(self, client, conn, freezer):
        freezer.tick(60)
        conn.execute(
            "INSERT INTO audit_log (media_item_id, action, created_at, actor)"
            " VALUES ('_security', 'sec:password.changed', ?, 'viewer')",
            ("2026-03-14T09:27:00+00:00",),
        )
        conn.commit()

        changed = password_changed_at(conn, "viewer")

        assert changed is not None
        assert changed.isoformat() == "2026-03-14T09:27:00+00:00"


class TestForcedPage:
    def _told(self, resp):
        assert resp.status_code == 200
        body = json.loads(resp.text)
        return body["field"], body["rules"], body["error"] or ""

    def test_the_first_visit_has_no_field_at_fault(self, page):
        assert self._told(page.get(_PAGE)) == (None, [], "")

    @pytest.mark.parametrize(
        ("form", "field"),
        [
            ({"old": "", "new": ""}, "old_password"),
            ({"old": _OLD, "new": ""}, "new_password"),
            ({"again": ""}, "confirm_password"),
            ({"again": "Harbour-glass-28"}, "confirm_password"),
        ],
    )
    def test_an_empty_or_different_field_is_the_one_at_fault(self, page, form, field):
        assert self._told(_submit(page, **form))[:2] == (field, [])

    def test_a_wrong_current_password_is_said_about_its_field(self, page):
        field, rules, error = self._told(_submit(page, old="wrong"))

        assert (field, rules) == ("old_password", [])
        assert "incorrect" in error

    def test_each_refused_rule_is_named(self, page):
        field, rules, _ = self._told(_submit(page, new="viewerviewer"))

        assert (field, rules) == ("new_password", ["repeats", "username", "mix"])

    @pytest.mark.parametrize("new", [_OLD, "Ｃ" + _OLD[1:]], ids=["typed", "full-width"])
    def test_the_password_already_in_use_is_refused(self, page, conn, new):
        field, rules, _ = self._told(_submit(page, new=new))

        assert (field, rules) == ("new_password", ["same"])
        assert page.get("/watch", follow_redirects=False).headers["location"] == _PAGE

    def test_the_lock_is_said_and_the_right_password_is_not_confirmed(self, page, conn):
        _lock(conn)
        wrong, right = _submit(page, old="wrong"), _submit(page)

        assert right.text == wrong.text
        assert json.loads(right.text)["locked"] is True
        field, _, error = self._told(right)
        assert field == "old_password"
        assert "15 minutes" in error
        assert verify_password(conn, "viewer", _OLD)

    def test_over_the_limit_no_field_is_at_fault(self, page):
        for _ in range(6):
            resp = page.post(_PAGE, data={})

        field, _, error = self._told(resp)
        assert field is None
        assert error.startswith("Too many attempts")

    def test_a_good_one_opens_watch(self, page):
        resp = _submit(page)

        assert (resp.status_code, resp.headers["location"]) == (302, "/watch")
